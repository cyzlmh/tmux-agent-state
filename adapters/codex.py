#!/usr/bin/env python3
"""Codex shared-daemon routing, without inferring state from terminal text.

The launcher transparently relays the TUI's Unix WebSocket connection. Only
responses to this client's thread/start, resume and fork requests establish a
binding; broadcast notifications never do. Native turn/status/approval events
report shared-daemon state even when remote-client hooks are disabled. Local
hooks can also resolve session_id using the binding. No third-party dependencies.
"""
from __future__ import annotations

import hashlib
import json
import os
import select
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_MESSAGE = 16 * 1024 * 1024
OWNER_OPTION = "@agent-codex-owner"


def route_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "tmux-agent-state"


def route_path(thread: str) -> Path:
    return route_dir() / (hashlib.sha256(thread.encode()).hexdigest() + ".json")


def tmux(args: list[str], command: list[str]) -> str:
    r = subprocess.run(command + args, capture_output=True, text=True, timeout=3, check=False)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "tmux command failed")
    return r.stdout.strip()


def pane_exists(pane: str, command: list[str]) -> bool:
    return pane in tmux(["list-panes", "-a", "-F", "#{pane_id}"], command).splitlines()


def process_identity(pid: int) -> str:
    r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                       capture_output=True, text=True, timeout=2, check=True)
    if not r.stdout.strip():
        raise RuntimeError("Codex launcher process disappeared")
    return r.stdout.strip()


def run_state(command: list[str], pane: str, args: list[str]) -> None:
    env = dict(os.environ, TMUX_PANE=pane, TMUX_STATUS_TMUX=" ".join(command))
    subprocess.run(["bash", str(HERE / "agent-state.sh"), "--agent", "codex", *args], env=env,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=5, check=True)


def write_state(command: list[str], pane: str, state: str, detail: str) -> None:
    run_state(command, pane, ["--state", state, "--detail", detail])


class Binding:
    def __init__(self, command: list[str], pane: str):
        self.command, self.pane = command, pane
        self.owner = uuid.uuid4().hex
        self.process_start = process_identity(os.getpid())
        self.thread: str | None = None
        self.paths: set[Path] = set()
        self.lock = threading.RLock()

    def select(self, thread: str) -> bool:
        with self.lock:
            if thread == self.thread:
                return False
            root = route_dir()
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
                raise RuntimeError(f"routing directory must be private and owned by you: {root}")
            data = {"thread": thread, "pane": self.pane, "tmux": self.command,
                    "owner": self.owner, "pid": os.getpid(), "process_start": self.process_start}
            # Invalidate the old session *before* publishing the new binding.
            tmux(["set-option", "-p", "-t", self.pane, OWNER_OPTION,
                  json.dumps({"thread": thread, "owner": self.owner})], self.command)
            path = route_path(thread)
            fd, tmp = tempfile.mkstemp(dir=root)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            self.paths.add(path)
            self.thread = thread
            return True

    def owns_pane(self) -> bool:
        marker = json.loads(tmux(["show-options", "-pqv", "-t", self.pane, OWNER_OPTION], self.command) or "{}")
        return marker == {"thread": self.thread, "owner": self.owner}

    def close(self) -> None:
        with self.lock:
            try:
                owner = json.loads(tmux(["show-options", "-pqv", "-t", self.pane,
                                         OWNER_OPTION], self.command) or "{}")
                if owner.get("owner") == self.owner:
                    raw = tmux(["show-options", "-pqv", "-t", self.pane, "@agent-state"], self.command)
                    if raw and json.loads(raw).get("tool") == "codex":
                        run_state(self.command, self.pane, ["--clear"])
                    tmux(["set-option", "-u", "-p", "-t", self.pane, OWNER_OPTION], self.command)
            except (OSError, RuntimeError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
                pass
            for path in self.paths:
                try:
                    if json.loads(path.read_text()).get("owner") == self.owner:
                        path.unlink()
                except (OSError, ValueError):
                    pass
            # SIGKILL/crashes still leave stale state. Normal detach clears
            # only this launcher's Codex state, never another adapter's value.


class Observer:
    """Native JSON-RPC state reporting; hooks can be disabled for --remote TUIs.

    Only the selected thread's lifecycle/status events and blocking requests
    affect state. Tokens, tools and broadcasts for other threads are ignored.
    """
    BLOCKING = frozenset({"item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                          "item/permissions/requestApproval", "mcpServer/elicitation/request",
                          "item/tool/requestUserInput"})

    def __init__(self, binding: Binding):
        self.binding = binding
        self.pending: dict[str, str] = {}
        self.requests: dict[str, str] = {}
        self.state: tuple[str, str] | None = None
        self.turn: str | None = None
        self.active = False
        self.saw_turn = False
        self.lock = threading.RLock()

    def set_state(self, state: str, detail: str) -> None:
        if self.state != (state, detail) and self.binding.owns_pane():
            write_state(self.binding.command, self.binding.pane, state, detail)
            self.state = state, detail

    def status(self, status: dict) -> None:
        kind = status.get("type")
        if kind == "active":
            self.active = True
            self.saw_turn = True
            flags = status.get("activeFlags") or []
            if self.requests or "waitingOnApproval" in flags or "waitingOnUserInput" in flags:
                self.set_state("waiting", "asking")
            else:
                self.set_state("busy", "working")
        elif kind == "systemError":
            self.active = False
            self.turn = None
            self.requests.clear()
            self.set_state("waiting", "error")
        elif kind in ("idle", "notLoaded"):
            self.active = False
            self.turn = None
            # An idle notification following a failed completion isn't success.
            detail = self.state[1] if self.state and self.state[1] in ("error", "done") else "done" if self.saw_turn else "ready"
            self.requests.clear()
            self.set_state("waiting", detail)

    def resolved(self, request_id: str) -> None:
        thread = self.requests.pop(request_id, None)
        if thread == self.binding.thread:
            if self.requests:
                self.set_state("waiting", "asking")
            elif self.active:
                self.set_state("busy", "working")
            else:
                self.set_state("waiting", "done" if self.saw_turn else "ready")

    def event(self, data: dict) -> None:
        params = data.get("params") or {}
        if not self.binding.thread or params.get("threadId") != self.binding.thread:
            return
        method = data.get("method")
        if method == "turn/started":
            self.turn = (params.get("turn") or {}).get("id")
            self.active = True
            self.saw_turn = True
            self.requests.clear()
            self.set_state("busy", "working")
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            if self.turn and turn.get("id") != self.turn:
                return  # A late completion from an earlier turn is not idle.
            if turn.get("status") not in ("completed", "interrupted", "failed"):
                return
            self.turn = None
            self.active = False
            self.saw_turn = True
            self.requests.clear()
            self.set_state("waiting", "error" if turn["status"] == "failed" else "done")
        elif method == "thread/status/changed":
            self.status(params.get("status") or {})
        elif method in self.BLOCKING and "id" in data:
            if method == "item/tool/requestUserInput" and params.get("isBlocking") is not True:
                return
            if self.turn and params.get("turnId") and params["turnId"] != self.turn:
                return
            self.turn = self.turn or params.get("turnId")
            self.active = self.active or bool(self.turn)
            self.requests[json.dumps(data["id"])] = self.binding.thread
            self.set_state("waiting", "asking")
        elif method == "serverRequest/resolved":
            self.resolved(json.dumps(params.get("requestId")))

    def message(self, incoming: bool, message: bytes) -> None:
        try:
            data = json.loads(message)
            if not isinstance(data, dict):
                return
            with self.lock:
                method = data.get("method")
                if not incoming:
                    if method in ("thread/start", "thread/resume", "thread/fork") and "id" in data:
                        self.pending[json.dumps(data["id"])] = method
                    elif method is None and ("result" in data or "error" in data):
                        self.resolved(json.dumps(data.get("id")))
                    return
                if method is not None:
                    self.event(data)
                    return
                request = self.pending.pop(json.dumps(data.get("id")), None)
                if request is None or "error" in data:
                    return
                result = (data.get("result") or {}).get("thread") or {}
                thread = result.get("id")
                if isinstance(thread, str) and thread:
                    changed = self.binding.select(thread)
                    if not changed and self.state is not None:
                        return  # Late attach metadata must not reset live state.
                    self.requests.clear()
                    self.turn = None
                    self.active = False
                    self.state = None
                    self.saw_turn = False
                    self.status(result.get("status") or {"type": "idle"})
        except (ValueError, TypeError, AttributeError, OSError, RuntimeError, subprocess.SubprocessError) as e:
            print(f"codex-tmux: routing: {e}", file=sys.stderr)


class Frames:
    """Incremental WebSocket observer; raw bytes are forwarded independently.

    Handles HTTP upgrades, masked frames, extended lengths and fragmented text.
    Control/binary frames are ignored. Inspection is bounded and fail-closed;
    an unsupported frame never changes the proxied traffic.
    """
    def __init__(self, observer: Observer, incoming: bool):
        self.observer, self.incoming = observer, incoming
        self.buffer = bytearray()
        self.http = True
        self.enabled = True
        self.fragment: bytearray | None = None

    def feed(self, chunk: bytes) -> None:
        if not self.enabled:
            return
        self.buffer.extend(chunk)
        if self.http:
            end = self.buffer.find(b"\r\n\r\n")
            if end < 0:
                if len(self.buffer) > 65536:
                    self.enabled = False
                    self.buffer.clear()
                return
            del self.buffer[:end + 4]
            self.http = False
        while len(self.buffer) >= 2:
            first, second = self.buffer[:2]
            opcode, fin = first & 15, bool(first & 128)
            length, offset = second & 127, 2
            extra = 2 if length == 126 else 8 if length == 127 else 0
            if len(self.buffer) < offset + extra:
                return
            if extra:
                length = int.from_bytes(self.buffer[offset:offset + extra], "big")
                offset += extra
            mask_len = 4 if second & 128 else 0
            if length > MAX_MESSAGE:
                self.enabled = False
                self.buffer.clear()
                self.fragment = None
                return
            if len(self.buffer) < offset + mask_len + length:
                return
            mask = self.buffer[offset:offset + mask_len]
            offset += mask_len
            payload = bytes(self.buffer[offset:offset + length])
            del self.buffer[:offset + length]
            if mask:
                payload = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
            if first & 0x70:  # No extensions are requested by Codex today.
                self.fragment = None
            elif opcode == 1:
                if fin:
                    self.observer.message(self.incoming, payload)
                else:
                    self.fragment = bytearray(payload)
            elif opcode == 0 and self.fragment is not None:
                self.fragment.extend(payload)
                if len(self.fragment) > MAX_MESSAGE:
                    self.fragment = None
                elif fin:
                    self.observer.message(self.incoming, bytes(self.fragment))
                    self.fragment = None
            elif opcode == 2:
                self.fragment = None


def relay(client: socket.socket, upstream: socket.socket, observer: Observer,
          stop: threading.Event) -> None:
    """One client per launcher. Observe each message before forwarding its bytes."""
    with client, upstream, selectors.DefaultSelector() as sel:
        client.settimeout(5)
        upstream.settimeout(5)
        sel.register(client, selectors.EVENT_READ, (upstream, Frames(observer, False)))
        sel.register(upstream, selectors.EVENT_READ, (client, Frames(observer, True)))
        while not stop.is_set():
            for key, _ in sel.select(0.2):
                target, frames = key.data
                chunk = key.fileobj.recv(65536)
                if not chunk:
                    return
                frames.feed(chunk)
                target.sendall(chunk)


def resolve(payload: dict) -> tuple[list[str], str] | None:
    thread = payload.get("session_id")
    if payload.get("agent_id") or not isinstance(thread, str) or not thread:
        return None
    path = route_path(thread)
    try:
        d = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(d, dict):
        raise TypeError("invalid Codex routing record")
    # A stale/malformed route must never fall back to the daemon's pane.
    root = path.parent
    if (root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077
            or path.is_symlink() or path.stat().st_uid != os.getuid()):
        raise RuntimeError("unsafe Codex routing file")
    if d.get("thread") != thread:
        raise RuntimeError("Codex routing thread mismatch")
    command, pane = d["tmux"], d["pane"]
    if (not isinstance(command, list) or not command or command[0] != "tmux"
            or not all(isinstance(x, str) for x in command)
            or not isinstance(pane, str) or not pane.startswith("%")):
        raise RuntimeError("invalid Codex routing target")
    os.kill(d["pid"], 0)
    if process_identity(d["pid"]) != d.get("process_start"):
        raise RuntimeError("Codex launcher PID was reused")
    if not pane_exists(pane, command):
        raise RuntimeError("Codex pane no longer exists")
    owner = json.loads(tmux(["show-options", "-pqv", "-t", pane, OWNER_OPTION], command) or "{}")
    if owner != {"thread": thread, "owner": d["owner"]}:
        raise RuntimeError("Codex routing owner changed")
    return command, pane


def local_pane(command: list[str]) -> str | None:
    """Only real ancestry proves local hooks; inherited TMUX_PANE is not proof."""
    rows = tmux(["list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}"], command)
    panes = {r.split("\t")[1]: r.split("\t")[0] for r in rows.splitlines() if "\t" in r}
    pid = str(os.getpid())
    seen = set()
    while pid.isdigit() and int(pid) > 1 and pid not in seen:
        seen.add(pid)
        if pid in panes:
            return panes[pid]
        r = subprocess.run(["ps", "-o", "ppid=,command=", "-p", pid], capture_output=True, text=True, timeout=2, check=False)
        fields = r.stdout.strip().split(None, 1)
        if len(fields) != 2:
            return None
        if "app-server" in fields[1] and "codex" in fields[1]:
            return None  # Even a daemon started in a live pane is not its TUI.
        pid = fields[0]
    return None


def hook(args: list[str]) -> int:
    try:
        # Hooks send a complete JSON object and close stdin. Bound malformed
        # inputs; don't inspect user prompts or transcripts for routing.
        ready, _, _ = select.select([sys.stdin], [], [], 1)
        if not ready:
            return 0
        raw = sys.stdin.buffer.read(MAX_MESSAGE + 1)
        if len(raw) > MAX_MESSAGE:
            return 0
        payload = json.loads(raw)
        if (not isinstance(payload, dict) or payload.get("agent_id")
                or not isinstance(payload.get("session_id"), str) or not payload["session_id"]):
            return 0
        command = ["tmux", "-S", os.environ["TMUX"].split(",")[0]] if os.environ.get("TMUX") else ["tmux"]
        if os.environ.get("TMUX_STATUS_TMUX"):
            command = os.environ["TMUX_STATUS_TMUX"].split()
        try:
            pane = local_pane(command)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pane = None  # A daemon may have no tmux environment/server at all.
        # Proven local ancestry also lets --no-daemon resume a thread whose
        # previous shared-daemon launcher crashed and left a stale route.
        target = (command, pane) if pane else resolve(payload)
        if target is None:
            return 0
        command, pane = target
        env = dict(os.environ, TMUX_PANE=pane, TMUX_STATUS_TMUX=" ".join(command))
        result = subprocess.run(["bash", str(HERE / "agent-state.sh"), *args], env=env,
                                stdin=subprocess.DEVNULL, timeout=10, check=False)
        if result.returncode:
            print("codex-tmux: hook write failed; agent execution is unaffected", file=sys.stderr)
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as e:
        print(f"codex-tmux: hook skipped: {e}", file=sys.stderr)
        return 0  # Never fail the agent's turn for an adapter problem.


def launch(args: list[str]) -> int:
    binary = os.environ.get("TMUX_AGENT_CODEX_BIN", "codex")
    executable = shutil.which(binary)
    if executable and Path(executable).resolve() == HERE / "codex-tmux":
        raise RuntimeError("codex resolves to this launcher; set TMUX_AGENT_CODEX_BIN to the real Codex binary")
    # CLI utility commands and explicitly local invocations need no relay.
    commands = {"exec", "e", "review", "login", "logout", "mcp", "plugin", "app-server",
                "remote-control", "app", "completion", "update", "doctor", "sandbox", "debug",
                "apply", "queue", "archive", "delete", "migrate-rollouts", "unarchive", "cloud",
                "exec-server", "features", "help"}
    if (not os.environ.get("TMUX_PANE") or any(x in args for x in ("--help", "-h", "--version", "-V", "--no-daemon"))
            or (args and args[0] in commands)):
        os.execvp(binary, [binary, *args])
    if any(x == "--remote" or x.startswith("--remote=") for x in args):
        raise RuntimeError("codex-tmux supports the local shared daemon, not --remote; use codex directly")
    # --remote does not inherit the client's cwd. Resolve directory overrides
    # here, but leave resumed/forked sessions in their recorded directory.
    args = args.copy()
    value_options = {"-c", "--config", "--enable", "--disable", "--remote-auth-token-env",
                     "-i", "--image", "-m", "--model", "--local-provider", "-p", "--profile",
                     "-s", "--sandbox", "-a", "--ask-for-approval", "-C", "--cd", "--add-dir"}
    action, has_cwd, skip_value = None, False, False
    for i, arg in enumerate(args):
        if skip_value:
            skip_value = False
            continue
        if arg == "--":
            break
        if arg in ("-C", "--cd"):
            has_cwd = True
            if i + 1 < len(args) and args[i + 1] and not args[i + 1].startswith("-"):
                args[i + 1] = os.path.abspath(args[i + 1])
        elif arg.startswith(("--cd=", "-C")):
            has_cwd = True
            prefix = "--cd=" if arg.startswith("--cd=") else "-C=" if arg.startswith("-C=") else "-C"
            if arg[len(prefix):]:
                args[i] = prefix + os.path.abspath(arg[len(prefix):])
        elif action is None and not arg.startswith("-"):
            action = arg
        skip_value = arg in value_options
    if not has_cwd and action not in commands | {"resume", "fork", "agents", "a"}:
        args = ["--cd", os.getcwd(), *args]
    command = os.environ.get("TMUX_STATUS_TMUX", "tmux").split()
    if os.environ.get("TMUX") and not os.environ.get("TMUX_STATUS_TMUX"):
        command = ["tmux", "-S", os.environ["TMUX"].split(",")[0]]
    pane = os.environ["TMUX_PANE"]
    if not pane_exists(pane, command):
        raise RuntimeError("TMUX_PANE does not exist")
    version = json.loads(subprocess.check_output([binary, "app-server", "daemon", "version"], text=True, timeout=10))
    if version.get("status") != "running":
        subprocess.run([binary, "app-server", "daemon", "start"], check=True, timeout=30)
        version = json.loads(subprocess.check_output([binary, "app-server", "daemon", "version"], text=True, timeout=10))
    upstream_path = version.get("socketPath")
    if not upstream_path:
        raise RuntimeError("Codex did not report a daemon socketPath (requires Codex 0.161+)")
    if not isinstance(upstream_path, str):
        raise TypeError("invalid daemon socketPath")
    binding = Binding(command, pane)
    stop = threading.Event()
    # Short private socket path avoids macOS's 104-byte Unix path limit.
    directory = tempfile.mkdtemp(prefix="codex-tmux-", dir="/tmp")
    path = str(Path(directory) / "relay.sock")
    server = socket.socket(socket.AF_UNIX)
    server.bind(path)
    server.listen(1)
    server.settimeout(0.2)

    def serve() -> None:
        while not stop.is_set():
            try:
                client, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            upstream = None
            try:
                upstream = socket.socket(socket.AF_UNIX)
                upstream.settimeout(5)
                upstream.connect(upstream_path)
                # RPC ids are connection-local; don't reuse pending requests
                # from a disconnected transport when the TUI reconnects.
                relay(client, upstream, Observer(binding), stop)
            except (OSError, RuntimeError) as e:
                print(f"codex-tmux: relay: {e}", file=sys.stderr)
                client.close()
                if upstream is not None:
                    upstream.close()

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    process = None
    try:
        process = subprocess.Popen([binary, "--remote", "unix://" + path, *args])
        # The foreground process group delivers Ctrl-C to the TUI too.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, lambda sig, _: process.send_signal(sig))
        code = process.wait()
        return code if code >= 0 else 128 - code
    finally:
        stop.set()
        worker.join(timeout=15)
        server.close()
        binding.close()
        shutil.rmtree(directory, ignore_errors=True)


def main() -> int:
    try:
        if sys.argv[1:2] == ["hook"]:
            return hook(sys.argv[2:])
        return launch(sys.argv[1:])
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as e:
        print(f"codex-tmux: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
