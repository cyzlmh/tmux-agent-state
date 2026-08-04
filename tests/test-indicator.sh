#!/usr/bin/env bash
# Integration tests: the tmux status-bar statistics segment
# (statusbar/scripts/indicator.py) and the window-status chip formats, against a
# real tmux server on an isolated socket.
#
# stale is decided by process presence (no-heartbeat protocol): a pane whose
# foreground command is a shell counts as stale even with a fresh payload.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"

trap cleanup_test_server EXIT
setup_test_server "indicator"

NOW=$(date +%s)

# 1. no agents -> empty stats
out=$(run_indicator)
assert_empty "$out" "no agents -> empty"
pass "empty session"

# 2. state on a shell foreground -> stale (process gone, no heartbeat)
write_state "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"done\"}"
out=$(run_indicator)
echo "$out" | grep -q '!1' || fail "shell pane with state should be stale: $out"
echo "$out" | grep -q 'colour161' || fail "stale should be red: $out"
pass "shell foreground -> stale"

# 3. same state with a live foreground process -> trusted (done)
hold_pane "$PANE" || fail "could not hold pane"
out=$(run_indicator)
echo "$out" | grep -q '✓1' || fail "live process should trust done state: $out"
pass "live process -> trust state"

# 4. counts: hold two more panes, asking + done + busy
P2="$(tmux_cmd split-window -d -P -F '#{pane_id}' -t ai:main)"
P3="$(tmux_cmd split-window -d -P -F '#{pane_id}' -t ai:main)"
hold_pane "$P2" || fail "hold P2"
hold_pane "$P3" || fail "hold P3"
tmux_cmd set-option -p -t "$P2" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"asking\"}"
tmux_cmd set-option -p -t "$P3" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"busy\",\"ts\":$NOW,\"detail\":\"working\"}"
out=$(run_indicator)
echo "$out" | grep -q '?1' || fail "should count 1 asking: $out"
echo "$out" | grep -q '✓1' || fail "should count 1 done: $out"
echo "$out" | grep -q '▶1' || fail "should count 1 running: $out"
echo "$out" | grep -q 'colour214' || fail "asking should be yellow: $out"
pass "counts: asking + done + running"

# 5. window scope: only the current window's panes
tmux_cmd new-window -d -t ai -n other
OTHER_PANE="$(tmux_cmd display-message -p -t ai:other.0 '#{pane_id}')"
hold_pane "$OTHER_PANE" || fail "hold other"
tmux_cmd set-option -p -t "$OTHER_PANE" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"done\"}"
tmux_cmd set -g @agent-status-scope window
out=$(run_indicator)
echo "$out" | grep -q '✓1' || fail "window scope should show main window only: $out"
if echo "$out" | grep -q '✓2'; then
    fail "window scope leaked other window's agent: $out"
fi
tmux_cmd set -g @agent-status-scope session
out=$(run_indicator)
echo "$out" | grep -q '✓2' || fail "session scope should include other window: $out"
pass "window vs session scope"

# 6. bootstrap: status interpolation + window-status formats + old hooks gone
tmux_cmd set -g status-right '#{agent_status} | %H:%M'
tmux_cmd run-shell "$ROOT_DIR/statusbar/statusbar.tmux"
val=$(tmux_cmd show-option -gqv status-right)
echo "$val" | grep -q 'indicator.py' || fail "bootstrap did not interpolate: $val"
if echo "$val" | grep -q '#{agent_status}'; then
    fail "placeholder not replaced: $val"
fi
wfmt=$(tmux_cmd show-option -gqv window-status-format)
echo "$wfmt" | grep -q '@agent-status-chips' || fail "window-status-format missing chips: $wfmt"
wcfmt=$(tmux_cmd show-option -gqv window-status-current-format)
echo "$wcfmt" | grep -q '@agent-status-chips' || fail "current format missing chips: $wcfmt"
hooks=$(tmux_cmd show-hooks -g 2>/dev/null || true)
if echo "$hooks" | grep -q 'pane-focus-in.sh'; then
    fail "old focus hooks not cleaned up"
fi
pass "bootstrap: interpolation + formats + cleanup"

echo "PASS: test-indicator"
