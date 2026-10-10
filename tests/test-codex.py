#!/usr/bin/env python3
"""Codex routing/transparent WebSocket relay regression tests (stdlib only)."""
import importlib.util
import inspect
import io
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("codex_adapter", ROOT / "adapters/codex.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def frame(payload, *, mask=False, opcode=1, fin=True):
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode()
    header = bytes([(128 if fin else 0) | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([length | (128 if mask else 0)])
    elif length < 65536:
        header += bytes([126 | (128 if mask else 0)]) + struct.pack(">H", length)
    else:
        header += bytes([127 | (128 if mask else 0)]) + struct.pack(">Q", length)
    if mask:
        key = b"1234"
        return header + key + bytes(v ^ key[i % 4] for i, v in enumerate(payload))
    return header + payload


def recv_exact(sock, size):
    data = b""
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise RuntimeError("unexpected EOF")
        data += part
    return data


def recv_message(sock):
    _first, second = recv_exact(sock, 2)
    size = second & 127
    if size in (126, 127):
        size = int.from_bytes(recv_exact(sock, 2 if size == 126 else 8), "big")
    key = recv_exact(sock, 4) if second & 128 else b""
    data = recv_exact(sock, size)
    if key:
        data = bytes(v ^ key[i % 4] for i, v in enumerate(data))
    return json.loads(data)


def recv_http(sock):
    data = b""
    while not data.endswith(b"\r\n\r\n"):
        data += recv_exact(sock, 1)
    return data


class FrameTests(unittest.TestCase):
    def test_bytewise_masked_fragmented_and_control(self):
        observer = Mock()
        frames = adapter.Frames(observer, False)
        stream = (b"GET / HTTP/1.1\r\nHost: local\r\n\r\n"
                  + frame(b'{"id":', mask=True, fin=False)
                  + frame(b"ping", mask=True, opcode=9)
                  + frame(b'1}', mask=True, opcode=0))
        for byte in stream:
            frames.feed(bytes([byte]))
        observer.message.assert_called_once_with(False, b'{"id":1}')

    def test_extended_sizes_coalesced(self):
        observer = Mock()
        frames = adapter.Frames(observer, True)
        a, b = b"a" * 300, b"b" * 70000
        frames.feed(b"HTTP/1.1 101 Switching Protocols\r\n\r\n" + frame(a) + frame(b))
        self.assertEqual(observer.message.call_args_list[0].args, (True, a))
        self.assertEqual(observer.message.call_args_list[1].args, (True, b))

    def test_oversized_and_compressed_not_observed(self):
        observer = Mock()
        frames = adapter.Frames(observer, True)
        frames.feed(b"HTTP/1.1 101 OK\r\n\r\n" + b"\xc1\x02{}")
        observer.message.assert_not_called()
        frames.feed(b"\x81\x7f" + struct.pack(">Q", adapter.MAX_MESSAGE + 1))
        self.assertFalse(frames.enabled)
        self.assertFalse(frames.buffer)

    def test_relay_preserves_raw_bytes(self):
        a, client = socket.socketpair()
        upstream, b = socket.socketpair()
        a.settimeout(3)
        b.settimeout(3)
        observer = Mock()
        stop = threading.Event()
        worker = threading.Thread(target=adapter.relay, args=(client, upstream, observer, stop))
        worker.start()
        try:
            request = b"GET / HTTP/1.1\r\n\r\n" + frame({"id": 1}, mask=True)
            a.sendall(request)
            self.assertEqual(recv_exact(b, len(request)), request)
            response = b"HTTP/1.1 101 OK\r\n\r\n" + frame({"result": {}})
            b.sendall(response)
            self.assertEqual(recv_exact(a, len(response)), response)
        finally:
            stop.set()
            worker.join(3)
            a.close()
            b.close()
        self.assertFalse(worker.is_alive())


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"CODEX_HOME": self.tmp.name}).start()
        self.options = {}
        self.command = ["tmux", "-L", "test-socket"]
        self.tmux = patch.object(adapter, "tmux", side_effect=self.fake_tmux).start()

    def fake_tmux(self, args, command):
        self.assertEqual(command, self.command)
        if args[0] == "list-panes":
            return "%1\n%2\n%3"
        pane = args[args.index("-t") + 1]
        if args[0] == "show-options":
            return self.options.get(pane, "")
        if "-u" in args:
            self.options.pop(pane, None)
        else:
            self.options[pane] = args[-1]
        return ""

    def test_multiple_panes_and_socket_routing(self):
        a, b = adapter.Binding(self.command, "%1"), adapter.Binding(self.command, "%2")
        a.select("thread-a")
        b.select("thread-b")
        with patch.dict(os.environ, {"TMUX_PANE": "%3", "TMUX": "/bad/socket,1,1"}):
            self.assertEqual(adapter.resolve({"session_id": "thread-a"}), (self.command, "%1"))
            self.assertEqual(adapter.resolve({"session_id": "thread-b"}), (self.command, "%2"))
        self.assertIsNone(adapter.resolve({"session_id": "unbound"}))
        self.assertIsNone(adapter.resolve({"session_id": "thread-a", "agent_id": "child"}))

    def test_switch_invalidates_old_thread_and_cleanup(self):
        binding = adapter.Binding(self.command, "%1")
        binding.select("old")
        binding.select("new")
        with self.assertRaisesRegex(RuntimeError, "owner changed"):
            adapter.resolve({"session_id": "old"})
        self.assertEqual(adapter.resolve({"session_id": "new"}), (self.command, "%1"))
        binding.close()
        self.assertFalse(adapter.route_path("old").exists())
        self.assertFalse(adapter.route_path("new").exists())
        self.assertNotIn("%1", self.options)

    def test_old_launcher_cleanup_does_not_remove_new_binding(self):
        a, b = adapter.Binding(self.command, "%1"), adapter.Binding(self.command, "%1")
        a.select("thread")
        b.select("thread")
        a.close()
        self.assertEqual(adapter.resolve({"session_id": "thread"}), (self.command, "%1"))
        self.assertEqual(json.loads(self.options["%1"])["owner"], b.owner)

    def test_stale_pid_dead_pane_and_tampered_owner(self):
        binding = adapter.Binding(self.command, "%1")
        binding.select("thread")
        with patch.object(adapter.os, "kill", side_effect=ProcessLookupError), self.assertRaises(ProcessLookupError):
            adapter.resolve({"session_id": "thread"})
        with patch.object(adapter, "process_identity", return_value="different start"), self.assertRaisesRegex(RuntimeError, "PID was reused"):
            adapter.resolve({"session_id": "thread"})
        with patch.object(adapter, "pane_exists", return_value=False), self.assertRaisesRegex(RuntimeError, "no longer exists"):
            adapter.resolve({"session_id": "thread"})
        self.options["%1"] = json.dumps({"thread": "thread", "owner": "other"})
        with self.assertRaisesRegex(RuntimeError, "owner changed"):
            adapter.resolve({"session_id": "thread"})

    def test_close_only_clears_its_own_codex_state(self):
        binding = adapter.Binding(self.command, "%1")
        binding.select("thread")
        original = self.fake_tmux
        for tool in ("pi", "codex"):
            def with_state(args, command, tool=tool):
                if args[0] == "show-options" and args[-1] == "@agent-state":
                    return json.dumps({"tool": tool})
                return original(args, command)
            with patch.object(adapter, "tmux", side_effect=with_state), patch.object(adapter, "run_state") as run:
                binding.close()
                if tool == "codex":
                    run.assert_called_once_with(self.command, "%1", ["--clear"])
                else:
                    run.assert_not_called()
            binding.thread = None
            binding.select("thread")

    def test_bad_payload_path_traversal_and_unsafe_dir(self):
        self.assertIsNone(adapter.resolve({"session_id": "../../outside"}))
        self.assertIsNone(adapter.resolve({"session_id": 42}))
        b = adapter.Binding(self.command, "%1")
        b.select("thread")
        adapter.route_dir().chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            adapter.resolve({"session_id": "thread"})

    def test_only_this_clients_successful_thread_responses_bind(self):
        binding = Mock()
        observer = adapter.Observer(binding)
        observer.message(True, json.dumps({"method": "thread/started", "params": {"thread": {"id": "broadcast"}}}).encode())
        observer.message(False, b'{"id":1,"method":"thread/resume","params":{"threadId":"bad"}}')
        observer.message(True, b'{"id":1,"error":{"message":"missing"}}')
        observer.message(True, b'{"id":2,"result":{"thread":{"id":"other-client"}}}')
        binding.select.assert_not_called()
        observer.message(False, b'{"id":3,"method":"thread/start"}')
        with patch.object(adapter, "write_state") as write:
            observer.message(True, b'{"id":3,"result":{"thread":{"id":"mine"}}}')
            binding.select.assert_called_once_with("mine")
            write.assert_called_once()

    def test_resume_uses_reported_active_status(self):
        binding = Mock()
        for status, state, detail in [
            ({"type": "active", "activeFlags": []}, "busy", "working"),
            ({"type": "active", "activeFlags": ["waitingOnApproval"]}, "waiting", "asking"),
            ({"type": "systemError"}, "waiting", "error"),
        ]:
            observer = adapter.Observer(binding)
            observer.message(False, b'{"id":1,"method":"thread/resume"}')
            with patch.object(adapter, "write_state") as write:
                observer.message(True, json.dumps({"id": 1, "result": {"thread": {"id": "mine", "status": status}}}).encode())
                self.assertEqual(write.call_args.args[2:], (state, detail))

    def test_hook_routes_without_daemon_tmux_and_never_fails_turn(self):
        payload_file = Path(self.tmp.name) / "payload.json"
        payload_file.write_text('{"session_id":"mine"}')
        with payload_file.open() as stdin, patch.object(adapter.sys, "stdin", stdin), \
                patch.object(adapter, "local_pane", side_effect=RuntimeError("no default tmux server")), \
                patch.object(adapter, "resolve", return_value=(self.command, "%2")), \
                patch.object(adapter.subprocess, "run", return_value=Mock(returncode=1)) as run:
            with patch.object(adapter.sys, "stderr", io.StringIO()):
                self.assertEqual(adapter.hook(["--clear"]), 0)
            self.assertEqual(run.call_args.kwargs["env"]["TMUX_PANE"], "%2")
            self.assertEqual(run.call_args.kwargs["env"]["TMUX_STATUS_TMUX"], " ".join(self.command))

    def test_proven_local_hook_ignores_stale_mapping(self):
        payload_file = Path(self.tmp.name) / "payload.json"
        payload_file.write_text('{"session_id":"mine"}')
        with payload_file.open() as stdin, patch.object(adapter.sys, "stdin", stdin), \
                patch.object(adapter, "local_pane", return_value="%1"), \
                patch.object(adapter, "resolve", side_effect=RuntimeError("stale")) as resolve, \
                patch.object(adapter.subprocess, "run", return_value=Mock(returncode=0)) as run:
            self.assertEqual(adapter.hook(["--clear"]), 0)
            resolve.assert_not_called()
            self.assertEqual(run.call_args.kwargs["env"]["TMUX_PANE"], "%1")

    def test_native_turn_approval_and_completion_without_hooks(self):
        binding = adapter.Binding(self.command, "%1")
        binding.select("mine")
        observer = adapter.Observer(binding)
        def message(method, params, **extra):
            observer.message(True, json.dumps({"method": method, "params": {"threadId": "mine", **params}, **extra}).encode())
        with patch.object(adapter, "write_state") as write:
            message("turn/started", {"turn": {"id": "t1", "status": "inProgress"}})
            message("thread/status/changed", {"status": {"type": "active", "activeFlags": []}})
            message("item/agentMessage/delta", {"delta": "never used for state"})
            message("item/commandExecution/requestApproval", {"turnId": "t1"}, id=50)
            observer.message(False, b'{"id":50,"result":{"decision":"accept"}}')
            message("turn/completed", {"turn": {"id": "t1", "status": "completed"}})
            message("thread/status/changed", {"status": {"type": "idle"}})
            self.assertEqual([c.args[2:] for c in write.call_args_list], [
                ("busy", "working"), ("waiting", "asking"),
                ("busy", "working"), ("waiting", "done")])

    def test_native_failure_interrupt_and_late_events(self):
        binding = adapter.Binding(self.command, "%1")
        binding.select("mine")
        observer = adapter.Observer(binding)
        with patch.object(adapter, "write_state") as write:
            for data in [
                {"method": "turn/started", "params": {"threadId": "mine", "turn": {"id": "t2"}}},
                {"method": "turn/completed", "params": {"threadId": "foreign", "turn": {"id": "t2", "status": "completed"}}},
                {"method": "turn/completed", "params": {"threadId": "mine", "turn": {"id": "old", "status": "completed"}}},
                {"method": "turn/completed", "params": {"threadId": "mine", "turn": {"id": "t2", "status": "failed"}}},
                {"method": "thread/status/changed", "params": {"threadId": "mine", "status": {"type": "idle"}}},
                {"method": "turn/started", "params": {"threadId": "mine", "turn": {"id": "t3"}}},
                {"method": "turn/completed", "params": {"threadId": "mine", "turn": {"id": "t3", "status": "interrupted"}}},
            ]:
                observer.message(True, json.dumps(data).encode())
            self.assertEqual([c.args[2:] for c in write.call_args_list], [
                ("busy", "working"), ("waiting", "error"),
                ("busy", "working"), ("waiting", "done")])

    def test_repeat_attach_response_and_replaced_owner_cannot_overwrite(self):
        binding = adapter.Binding(self.command, "%1")
        binding.select("mine")
        observer = adapter.Observer(binding)
        with patch.object(adapter, "write_state") as write:
            observer.message(True, b'{"method":"turn/started","params":{"threadId":"mine","turn":{"id":"t"}}}')
            observer.message(False, b'{"id":10,"method":"thread/resume","params":{"threadId":"mine"}}')
            observer.message(True, b'{"id":10,"result":{"thread":{"id":"mine","status":{"type":"idle"}}}}')
            self.assertEqual(write.call_count, 1)
            self.options["%1"] = json.dumps({"thread": "other", "owner": "another launcher"})
            observer.message(True, b'{"method":"turn/completed","params":{"threadId":"mine","turn":{"id":"t","status":"completed"}}}')
            self.assertEqual(write.call_count, 1)

    def test_resumed_active_turn_resolves_nullable_elicitation_to_busy(self):
        binding = adapter.Binding(self.command, "%1")
        binding.select("mine")
        observer = adapter.Observer(binding)
        with patch.object(adapter, "write_state") as write:
            observer.status({"type": "active", "activeFlags": []})
            observer.message(True, b'{"id":5,"method":"mcpServer/elicitation/request","params":{"threadId":"mine","turnId":null}}')
            observer.message(False, b'{"id":5,"result":{"action":"accept"}}')
            self.assertEqual([c.args[2:] for c in write.call_args_list], [
                ("busy", "working"), ("waiting", "asking"), ("busy", "working")])

    def test_nonblocking_input_and_multiple_pending_requests(self):
        binding = adapter.Binding(self.command, "%1")
        binding.select("mine")
        observer = adapter.Observer(binding)
        with patch.object(adapter, "write_state") as write:
            observer.message(True, b'{"method":"turn/started","params":{"threadId":"mine","turn":{"id":"t"}}}')
            observer.message(True, b'{"id":1,"method":"item/tool/requestUserInput","params":{"threadId":"mine","turnId":"t","isBlocking":false}}')
            self.assertEqual(write.call_count, 1)
            for id in (2, 3):
                observer.message(True, json.dumps({"id": id, "method": "item/tool/requestUserInput", "params": {"threadId": "mine", "turnId": "t", "isBlocking": True}}).encode())
            observer.message(False, b'{"id":2,"result":{}}')
            self.assertEqual(write.call_args.args[2:], ("waiting", "asking"))
            observer.message(True, b'{"method":"serverRequest/resolved","params":{"threadId":"mine","requestId":3}}')
            self.assertEqual(write.call_args.args[2:], ("busy", "working"))

    def test_local_fallback_uses_ancestry_not_env_and_rejects_daemon(self):
        def panes(args, command):
            return "%1\t101\n%2\t102"
        with patch.object(adapter, "tmux", side_effect=panes), patch.object(adapter.os, "getpid", return_value=200):
            with patch.object(adapter.subprocess, "run", return_value=Mock(stdout="101 codex --no-daemon")):
                self.assertEqual(adapter.local_pane(self.command), "%1")
            with patch.object(adapter.subprocess, "run", return_value=Mock(stdout="101 codex app-server --managed-daemon")):
                self.assertIsNone(adapter.local_pane(self.command))


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"TMUX_PANE": "%1", "TMUX_AGENT_CODEX_BIN": "codex"}, clear=True).start()
        patch.object(adapter.shutil, "which", return_value="/usr/bin/codex").start()
        patch.object(adapter, "pane_exists", return_value=True).start()
        patch.object(adapter.subprocess, "check_output", return_value=json.dumps(
            {"status": "running", "socketPath": "/tmp/daemon.sock"})).start()
        patch.object(adapter, "Binding").start()
        patch.object(adapter.socket, "socket").start()
        patch.object(adapter.threading, "Thread").start()
        patch.object(adapter.signal, "signal").start()
        self.process = patch.object(adapter.subprocess, "Popen").start()
        self.process.return_value.wait.return_value = 0

    def launched_args(self, args):
        self.assertEqual(adapter.launch(args), 0)
        command = self.process.call_args.args[0]
        self.assertEqual(command[:2], ["codex", "--remote"])
        self.assertTrue(command[2].startswith("unix://"))
        return command[3:]

    def test_new_session_uses_client_cwd(self):
        for args in ([], ["initial prompt"], ["-m", "resume"],
                     ["--", "resume"], ["--", "--cd=/prompt"]):
            with self.subTest(args=args):
                self.assertEqual(self.launched_args(args), ["--cd", os.getcwd(), *args])

    def test_explicit_absolute_cwd_is_preserved(self):
        cwd = str(ROOT)
        for args in (["-C", cwd], ["--cd", cwd], ["--cd=" + cwd], ["-C" + cwd], ["-C=" + cwd],
                     ["resume", "--cd", cwd], ["fork", "-C", cwd]):
            with self.subTest(args=args):
                self.assertEqual(self.launched_args(args), args)

    def test_relative_cwd_is_resolved_on_client(self):
        for args, expected in [
            (["-C", "."], ["-C", os.getcwd()]),
            (["--cd", ".."], ["--cd", os.path.abspath("..")]),
            (["--cd=.."], ["--cd=" + os.path.abspath("..")]),
            (["-C.."], ["-C" + os.path.abspath("..")]),
            (["-C=.."], ["-C=" + os.path.abspath("..")]),
            (["resume", "-C", "."], ["resume", "-C", os.getcwd()]),
        ]:
            with self.subTest(args=args):
                self.assertEqual(self.launched_args(args), expected)

    def test_resume_and_fork_keep_session_cwd(self):
        for action in ("resume", "fork"):
            for args in ([action, "--last"], ["-m", "model", action, "session-id"],
                         ["-c", "model='resume'", action, "--all"]):
                with self.subTest(args=args):
                    self.assertEqual(self.launched_args(args), args)


class LauncherIntegration(unittest.TestCase):
    """Real relay/launcher + real tmux, fake Codex protocol (no inference)."""
    @unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
    def test_shared_daemon_native_events_and_hook_targeting(self):
        with tempfile.TemporaryDirectory(prefix="cx-test-", dir="/tmp") as directory:
            root = Path(directory)
            client_cwd = root / "client project"
            client_cwd.mkdir()
            sock = root / "daemon.sock"
            tmux_sock = root / "tmux.sock"
            command = ["tmux", "-S", str(tmux_sock)]
            env = dict(os.environ, CODEX_HOME=str(root / "home"), TMUX_AGENT_STATE_LOG="",
                       TMUX_STATUS_COLORIZE="", TMUX_STATUS_TMUX=" ".join(command))
            subprocess.run(command + ["-f", "/dev/null", "new-session", "-d", "-s", "test"], check=True, env=env)
            try:
                pane = subprocess.check_output(command + ["display-message", "-p", "#{pane_id}"], text=True).strip()
                other = subprocess.check_output(command + ["split-window", "-d", "-P", "-F", "#{pane_id}"], text=True).strip()
                subprocess.run(command + ["set-option", "-p", "-t", other, "@agent-state", "untouched"], check=True)
                env.update(TMUX_PANE=pane, TMUX="/irrelevant/socket,1,0", TEST_OTHER_PANE=other,
                           TEST_DAEMON_SOCKET=str(sock), TEST_ROOT=str(ROOT))
                fake = root / "codex"
                fake.write_text('''#!/usr/bin/env python3
import sys, os, socket, json, subprocess
sys.path.insert(0, os.environ["TEST_ROOT"] + "/tests")
from test_codex_helpers import frame, recv_message, recv_http
if sys.argv[1:4] == ["app-server", "daemon", "version"]:
 print(json.dumps({"status":"running", "socketPath":os.environ["TEST_DAEMON_SOCKET"]})); sys.exit(0)
s = socket.socket(socket.AF_UNIX); s.settimeout(5)
s.connect(sys.argv[sys.argv.index("--remote")+1].removeprefix("unix://"))
s.sendall(b"GET / HTTP/1.1\\r\\nHost: localhost\\r\\n\\r\\n")
recv_http(s)
cwd = sys.argv[sys.argv.index("--cd")+1] if "--cd" in sys.argv else None
s.sendall(frame({"id":1,"method":"thread/start","params":{"cwd":cwd}}, mask=True))
assert recv_message(s)["result"]["thread"]["id"] == "mine"
cmd = os.environ["TMUX_STATUS_TMUX"].split()
def state(pane):
 return subprocess.check_output(cmd+["show-options","-pqv","-t",pane,"@agent-state"],text=True).strip()
assert json.loads(state(os.environ["TMUX_PANE"]))["detail"] == "ready"
# The --remote session disables hooks: native events must drive a full turn.
s.sendall(frame({"id":2,"method":"turn/start","params":{"threadId":"mine"}}, mask=True))
assert recv_message(s)["method"] == "turn/started"
assert json.loads(state(os.environ["TMUX_PANE"]))["state"] == "busy"
s.sendall(frame({"id":100,"method":"test/ack"}, mask=True))
assert recv_message(s)["method"] == "item/commandExecution/requestApproval"
assert json.loads(state(os.environ["TMUX_PANE"]))["detail"] == "asking"
s.sendall(frame({"id":50,"result":{"decision":"accept"}}, mask=True))
assert recv_message(s)["method"] == "thread/status/changed"
assert json.loads(state(os.environ["TMUX_PANE"]))["state"] == "busy"
s.sendall(frame({"id":101,"method":"test/ack"}, mask=True))
assert recv_message(s)["method"] == "turn/completed"
assert json.loads(state(os.environ["TMUX_PANE"]))["detail"] == "done"
assert state(os.environ["TEST_OTHER_PANE"]) == "untouched"
for args, expected in [(["--state","busy","--detail","working"],"busy"),(["--state","waiting","--detail","done"],"waiting")]:
 env = dict(os.environ,TMUX_PANE=os.environ["TEST_OTHER_PANE"])
 subprocess.run(["bash",os.environ["TEST_ROOT"]+"/adapters/agent-state.sh","--agent","codex","--codex-target",*args],input='{"session_id":"mine"}',text=True,env=env,check=True)
 assert json.loads(state(os.environ["TMUX_PANE"]))["state"] == expected
 assert state(os.environ["TEST_OTHER_PANE"]) == "untouched"
# Unbound child/foreign sessions must not inherit the daemon's live pane.
subprocess.run(["bash",os.environ["TEST_ROOT"]+"/adapters/agent-state.sh","--agent","codex","--codex-target","--clear"],input='{"session_id":"foreign"}',text=True,env=dict(os.environ,TMUX_PANE=os.environ["TEST_OTHER_PANE"]),check=True)
assert state(os.environ["TEST_OTHER_PANE"]) == "untouched"
s.close()
print("fake TUI: PASS")
''')
                fake.chmod(0o755)
                # Importable helpers for the fake TUI process.
                helpers = root / "test_codex_helpers.py"
                helpers.write_text("import json, struct\n" + "\n".join(
                    inspect.getsource(f) for f in (frame, recv_exact, recv_message, recv_http)))
                env.update(TMUX_AGENT_CODEX_BIN=str(fake), PYTHONPATH=str(root))
                errors = []
                server = socket.socket(socket.AF_UNIX)
                server.bind(str(sock))
                server.listen(1)
                server.settimeout(10)
                def serve():
                    try:
                        client, _ = server.accept()
                        with client:
                            client.settimeout(5)
                            recv_http(client)
                            client.sendall(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
                            start = recv_message(client)
                            self.assertEqual(start["method"], "thread/start")
                            self.assertEqual(start["params"]["cwd"], str(client_cwd.resolve()))
                            client.sendall(frame({"id":1,"result":{"thread":{"id":"mine", "status":{"type":"idle"}}}}))
                            self.assertEqual(recv_message(client)["method"], "turn/start")
                            client.sendall(frame({"method":"turn/started", "params":{"threadId":"mine","turn":{"id":"turn1","status":"inProgress"}}}))
                            self.assertEqual(recv_message(client)["id"], 100)
                            client.sendall(frame({"id":50,"method":"item/commandExecution/requestApproval", "params":{"threadId":"mine","turnId":"turn1"}}))
                            self.assertEqual(recv_message(client)["id"], 50)
                            client.sendall(frame({"method":"thread/status/changed", "params":{"threadId":"mine","status":{"type":"active","activeFlags":[]}}}))
                            self.assertEqual(recv_message(client)["id"], 101)
                            client.sendall(frame({"method":"turn/completed", "params":{"threadId":"mine","turn":{"id":"turn1","status":"completed"}}}))
                            while client.recv(1024):
                                pass
                    except (AssertionError, OSError, RuntimeError, ValueError) as e:
                        errors.append(e)
                worker = threading.Thread(target=serve)
                worker.start()
                try:
                    result = subprocess.run([str(ROOT / "adapters/codex-tmux")], env=env, cwd=client_cwd,
                                            capture_output=True, text=True, timeout=15, check=False)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("fake TUI: PASS", result.stdout)
                    self.assertFalse(list((root / "home/tmux-agent-state").glob("*.json")))
                finally:
                    worker.join(3)
                    server.close()
                self.assertFalse(worker.is_alive())
                self.assertFalse(errors, errors)
            finally:
                subprocess.run(command + ["kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    unittest.main()
