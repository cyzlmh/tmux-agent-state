#!/usr/bin/env python3
"""Unit tests for tmux-viz/tmux_viz.py (pure functions, no tmux)."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "tmux_viz", ROOT / "tmux-viz" / "tmux_viz.py"
)
viz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(viz)

passed = 0


def check(cond, msg):
    global passed
    assert cond, msg
    passed += 1


def pane(cmd, state="", dead="0"):
    return {"dead": dead, "cmd": cmd, "agent_state": state}


ASKING = '{"tool":"pi","state":"waiting","ts":1,"detail":"asking"}'   # ancient ts on purpose
DONE = '{"tool":"pi","state":"waiting","ts":1,"detail":"done"}'
BUSY = '{"tool":"claude","state":"busy","ts":1,"detail":"working"}'
READY = '{"tool":"pi","state":"waiting","ts":1,"detail":"ready"}'

# --- display_state (wire -> display, same mapping as indicator.py) ---
check(viz.display_state("busy", "working") == "running", "busy -> running")
check(viz.display_state("waiting", "asking") == "needs-input", "waiting+asking -> needs-input")
check(viz.display_state("waiting", "done") == "done", "waiting+done -> done")
check(viz.display_state("waiting", "ready") == "ready", "waiting+ready -> ready")
check(viz.display_state("waiting", "") == "ready", "waiting no detail -> ready")

# --- _classify: reader rules from PROTOCOL.md ---
# 1. dead always wins
c = viz._classify(pane("pi", ASKING, dead="1"))
check(c["state"] == "dead", "pane_dead wins over adapter")

# 2. adapter present + foreground not a shell -> trusted regardless of ts age
c = viz._classify(pane("node", ASKING))
check(c["state"] == "needs-input" and c["source"] == "adapter", f"live adapter asking: {c}")
c = viz._classify(pane("claude", DONE))
check(c["state"] == "done", f"live adapter done: {c}")
c = viz._classify(pane("node", BUSY))
check(c["state"] == "running" and c["tool"] == "claude", f"live adapter busy: {c}")
c = viz._classify(pane("node", READY))
check(c["state"] == "ready", f"live adapter ready: {c}")

# 3. adapter present + foreground is a shell -> stale (adapter process gone)
c = viz._classify(pane("zsh", DONE))
check(c["state"] == "stale" and c["tool"] == "pi", f"shell foreground -> stale: {c}")
check(viz._classify(pane("-zsh", DONE))["state"] == "stale", "login shell (-zsh) -> stale")
check(viz._classify(pane("bash", BUSY))["state"] == "stale", "bash -> stale")

# 4. no adapter + shell foreground -> shell (idle)
c = viz._classify(pane("zsh"))
check(c["state"] == "shell" and c["tool"] == "shell", f"plain shell: {c}")

# 5. otherwise -> untracked
c = viz._classify(pane("vim"))
check(c["state"] == "untracked", f"untracked: {c}")

# malformed / missing-ts payloads are not trusted
check(viz._classify(pane("node", "{bad"))["state"] == "untracked", "malformed -> untracked")
check(viz._classify(pane("node", '{"state":"busy"}'))["state"] == "untracked", "missing ts -> untracked")
check(viz._classify(pane("zsh", '{"state":"busy"}'))["state"] == "shell", "missing ts + shell -> shell")

# --- _agg: needs-input is the only attention state, it always leads ---
p = lambda s: {"state": s}
check(viz._agg([]) == "empty", "no panes -> empty")
check(viz._agg([p("done"), p("needs-input"), p("running")]) == "needs-input", "needs-input leads")
check(viz._agg([p("done"), p("stale"), p("running")]) == "stale", "stale over running/done")
check(viz._agg([p("done"), p("running")]) == "running", "running over done")
check(viz._agg([p("done"), p("shell")]) == "done", "done over shell")
check(viz._agg([p("dead"), p("dead")]) == "dead", "all dead -> dead")

# --- _io gating inputs ---
check(viz._io("") is None, "empty io -> None")
check(viz._io("{bad") is None, "malformed io -> None")
check(viz._io('{"output":"x"}') is None, "io without input -> None")
io = viz._io('{"input":"hi","output":"OK","ts":1}')
check(io == {"input": "hi", "output": "OK", "ts": 1}, f"io parse: {io}")

print(f"PASS: {passed} checks")
