#!/usr/bin/env python3
"""Unit tests for the shared classifier statusbar/scripts/agent_state.py and
the status segment's counting/rendering (pure functions, no tmux)."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ags = load("agent_state", ROOT / "statusbar" / "scripts" / "agent_state.py")
ind = load("indicator", ROOT / "statusbar" / "scripts" / "indicator.py")

passed = 0


def check(cond, msg):
    global passed
    assert cond, msg
    passed += 1


# --- parse_payload ---
p = ags.parse_payload('{"tool":"pi","state":"busy","ts":1718000000.5,"detail":"working"}')
check(p["tool"] == "pi" and p["state"] == "busy" and p["ts"] == 1718000000.5, f"parse busy: {p}")
check(ags.parse_payload("") is None, "empty -> None")
check(ags.parse_payload("{bad") is None, "malformed -> None")
check(ags.parse_payload('{"state":"bogus","ts":1}') is None, "unknown state -> None")
check(ags.parse_payload('{"state":"busy"}') is None, "missing ts -> None")

# --- display_state ---
check(ags.display_state("busy", "working") == "running", "busy -> running")
check(ags.display_state("waiting", "asking") == "needs-input", "waiting+asking -> needs-input")
check(ags.display_state("waiting", "done") == "done", "waiting+done -> done")
check(ags.display_state("waiting", "ready") == "ready", "waiting+ready -> ready")
check(ags.display_state("waiting", "") == "ready", "waiting no detail -> ready")

# --- classify: reader rules from PROTOCOL.md ---
DONE = '{"tool":"pi","state":"waiting","ts":1,"detail":"done"}'   # ancient ts, still fine
ASKING = '{"tool":"pi","state":"waiting","ts":1,"detail":"asking"}'
BUSY = '{"tool":"claude","state":"busy","ts":1,"detail":"working"}'
READY = '{"tool":"pi","state":"waiting","ts":1,"detail":"ready"}'

# 1. dead always wins
c = ags.classify("1", "pi", ASKING)
check(c["state"] == "dead" and c["rule"] == 1, "pane_dead wins over adapter")

# 2. adapter present + foreground not a shell -> trusted regardless of ts age
c = ags.classify("0", "node", ASKING)
check(c["state"] == "needs-input" and c["source"] == "adapter" and c["rule"] == 2,
      f"live adapter asking: {c}")
check(ags.classify("0", "claude", DONE)["state"] == "done", "live adapter done")
c = ags.classify("0", "node", BUSY)
check(c["state"] == "running" and c["tool"] == "claude", f"live adapter busy: {c}")
check(ags.classify("0", "node", READY)["state"] == "ready", "live adapter ready")

# 3. adapter present + foreground is a shell -> stale (adapter process gone)
c = ags.classify("0", "zsh", DONE)
check(c["state"] == "stale" and c["tool"] == "pi" and c["rule"] == 3, f"shell foreground: {c}")
check(ags.classify("0", "-zsh", DONE)["state"] == "stale", "login shell (-zsh) -> stale")
check(ags.classify("0", "bash", BUSY)["state"] == "stale", "bash -> stale")

# 4. no adapter + shell foreground -> shell (idle)
c = ags.classify("0", "zsh", "")
check(c["state"] == "shell" and c["tool"] == "shell" and c["rule"] == 4, f"plain shell: {c}")

# 5. otherwise -> untracked
c = ags.classify("0", "vim", "")
check(c["state"] == "untracked" and c["rule"] == 5, f"untracked: {c}")

# malformed / missing-ts payloads are not trusted
check(ags.classify("0", "node", "{bad")["state"] == "untracked", "malformed -> untracked")
check(ags.classify("0", "node", '{"state":"busy"}')["state"] == "untracked", "missing ts -> untracked")
check(ags.classify("0", "zsh", '{"state":"busy"}')["state"] == "shell", "missing ts + shell -> shell")

# --- indicator: count_stats / render_stats ---
entries = [
    (ASKING, "claude", "0"),
    (DONE, "claude", "0"),
    (DONE, "claude", "0"),
    (BUSY, "codex", "0"),
    (DONE, "zsh", "0"),      # shell -> stale
    (READY, "claude", "0"),  # ready: not counted
    (ASKING, "claude", "1"), # dead: not counted
    ("garbage", "claude", "0"),
    ("", "claude", "0"),
]
counts = ind.count_stats(entries)
check(counts == {"needs-input": 1, "done": 2, "stale": 1, "running": 1}, f"counts: {counts}")

r = ind.render_stats(counts, ind.DEFAULT_COLORS)
check("?1" in r and "colour180" in r and "bold" in r, f"needs-input segment: {r}")
check("✓2" in r and "colour108" in r, f"done segment: {r}")
check("!1" in r and "colour167" in r, f"stale segment: {r}")
check("▶1" in r and "colour68" in r, f"running segment: {r}")
# order fixed: needs-input, done, stale, running
check(r.find("?1") < r.find("✓2") < r.find("!1") < r.find("▶1"), f"order: {r}")

empty = ind.render_stats({"needs-input": 0, "done": 0, "stale": 0, "running": 0}, ind.DEFAULT_COLORS)
check(empty == "", "zero counts -> empty")
only_run = ind.render_stats({"needs-input": 0, "done": 0, "stale": 0, "running": 2}, ind.DEFAULT_COLORS)
check(only_run == "#[fg=colour68]▶2#[default]", f"only running: {only_run}")

print(f"PASS: {passed} checks")
