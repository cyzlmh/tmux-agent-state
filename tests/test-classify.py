#!/usr/bin/env python3
"""Unit tests for panel/scripts/indicator.py (pure functions, no tmux)."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "indicator", ROOT / "panel" / "scripts" / "indicator.py"
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

# --- classify (stale wins) ---
now = 1718000090.0  # 40s after ts below (within STALE=45s)
fresh = '{"tool":"pi","state":"busy","ts":1718000050,"detail":"working"}'
old = '{"tool":"pi","state":"busy","ts":1718000000,"detail":"working"}'
check(ind.classify(fresh, now) == "running", "fresh busy -> running")
check(ind.classify(old, now) == "stale", "old busy -> stale")

# --- count_stats / render_stats ---
entries = [
    '{"tool":"pi","state":"waiting","ts":1718000050,"detail":"asking"}',
    '{"tool":"pi","state":"waiting","ts":1718000050,"detail":"done"}',
    '{"tool":"pi","state":"waiting","ts":1718000050,"detail":"done"}',
    '{"tool":"pi","state":"busy","ts":1718000050,"detail":"working"}',
    '{"tool":"pi","state":"waiting","ts":1718000000,"detail":"done"}',  # stale
    '{"tool":"pi","state":"waiting","ts":1718000050,"detail":"ready"}',  # not counted
    "garbage",
    "",
]
counts = ind.count_stats(entries, now)
check(counts == {"needs-input": 1, "done": 2, "stale": 1, "running": 1}, f"counts: {counts}")

r = ind.render_stats(counts, ind.DEFAULT_COLORS)
check("?1" in r and "colour214" in r and "bold" in r, f"needs-input segment: {r}")
check("✓2" in r and "colour34" in r, f"done segment: {r}")
check("!1" in r and "colour161" in r, f"stale segment: {r}")
check("▶1" in r and "colour39" in r, f"running segment: {r}")
# order fixed: needs-input, done, stale, running
check(r.find("?1") < r.find("✓2") < r.find("!1") < r.find("▶1"), f"order: {r}")

empty = ind.render_stats({"needs-input": 0, "done": 0, "stale": 0, "running": 0}, ind.DEFAULT_COLORS)
check(empty == "", "zero counts -> empty")
only_run = ind.render_stats({"needs-input": 0, "done": 0, "stale": 0, "running": 2}, ind.DEFAULT_COLORS)
check(only_run == "#[fg=colour39]▶2#[default]", f"only running: {only_run}")

print(f"PASS: {passed} checks")
