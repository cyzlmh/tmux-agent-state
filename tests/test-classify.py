#!/usr/bin/env python3
"""Unit tests for statusbar/scripts/indicator.py (pure functions, no tmux)."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "indicator", ROOT / "statusbar" / "scripts" / "indicator.py"
)
ind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ind)

passed = 0


def check(cond, msg):
    global passed
    assert cond, msg
    passed += 1


# --- parse_state ---
p = ind.parse_state('{"tool":"pi","state":"busy","ts":1718000000.5,"detail":"working"}')
check(p == ("pi", "busy", "working", 1718000000.5), f"parse busy: {p}")
check(ind.parse_state("") is None, "empty -> None")
check(ind.parse_state("{bad") is None, "malformed -> None")
check(ind.parse_state('{"state":"bogus","ts":1}') is None, "unknown state -> None")
check(ind.parse_state('{"state":"busy"}') is None, "missing ts -> None")

# --- display_state ---
check(ind.display_state("busy", "working") == "running", "busy -> running")
check(ind.display_state("waiting", "asking") == "needs-input", "waiting+asking -> needs-input")
check(ind.display_state("waiting", "done") == "done", "waiting+done -> done")
check(ind.display_state("waiting", "ready") is None, "waiting+ready -> not counted")
check(ind.display_state("waiting", "") is None, "waiting no detail -> not counted")

# --- classify: stale by process presence (shell foreground), not by ts ---
done = '{"tool":"pi","state":"waiting","ts":1,"detail":"done"}'   # ancient ts, still fine
busy = '{"tool":"pi","state":"busy","ts":1,"detail":"working"}'
check(ind.classify(done, "zsh") == "stale", "shell foreground -> stale")
check(ind.classify(done, "-zsh") == "stale", "login shell (-zsh) -> stale")
check(ind.classify(done, "bash") == "stale", "bash -> stale")
check(ind.classify(done, "node") == "done", "agent process (node) -> trust state")
check(ind.classify(done, "claude") == "done", "agent process (claude) -> trust state")
check(ind.classify(busy, "claude") == "running", "busy + process -> running")
check(ind.classify("", "node") is None, "no state -> None regardless of process")

# --- count_stats / render_stats ---
entries = [
    ('{"tool":"pi","state":"waiting","ts":1,"detail":"asking"}', "claude"),
    ('{"tool":"pi","state":"waiting","ts":1,"detail":"done"}', "claude"),
    ('{"tool":"pi","state":"waiting","ts":1,"detail":"done"}', "claude"),
    ('{"tool":"pi","state":"busy","ts":1,"detail":"working"}', "codex"),
    ('{"tool":"pi","state":"waiting","ts":1,"detail":"done"}', "zsh"),   # shell -> stale
    ('{"tool":"pi","state":"waiting","ts":1,"detail":"ready"}', "claude"),  # not counted
    ("garbage", "claude"),
    ("", "claude"),
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
