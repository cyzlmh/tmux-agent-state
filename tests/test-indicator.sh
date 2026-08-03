#!/usr/bin/env bash
# Integration tests: the tmux status-bar statistics segment
# (panel/scripts/indicator.py) and the window-status chip formats, against a
# real tmux server on an isolated socket.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"

trap cleanup_test_server EXIT
setup_test_server "indicator"

NOW=$(date +%s)
OLD=$((NOW - 100))

# 1. no agents -> empty stats
out=$(run_indicator)
assert_empty "$out" "no agents -> empty"
pass "empty session"

# 2. counts: 2 done + 1 asking -> "?1 ✓2"
write_state "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"done\"}"
P2="$(tmux_cmd split-window -d -P -F '#{pane_id}' -t ai:main)"
tmux_cmd set-option -p -t "$P2" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"done\"}"
P3="$(tmux_cmd split-window -d -P -F '#{pane_id}' -t ai:main)"
tmux_cmd set-option -p -t "$P3" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"asking\"}"
out=$(run_indicator)
echo "$out" | grep -q '?1' || fail "should count 1 asking: $out"
echo "$out" | grep -q '✓2' || fail "should count 2 done: $out"
echo "$out" | grep -q 'colour214' || fail "asking should be yellow: $out"
pass "counts + colours"

# 3. busy + stale + ready: running counted, ready not
tmux_cmd set-option -p -t "$P2" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"busy\",\"ts\":$NOW,\"detail\":\"working\"}"
tmux_cmd set-option -p -t "$P3" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$OLD,\"detail\":\"done\"}"
tmux_cmd set-option -p -t "$PANE" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"ready\"}"
out=$(run_indicator)
echo "$out" | grep -q '▶1' || fail "should count 1 running: $out"
echo "$out" | grep -q '!1' || fail "should count 1 stale: $out"
echo "$out" | grep -q '✓' && fail "ready should not be counted: $out" || true
pass "running + stale counted, ready not"

# 3b. indicator doubles as the chips refresher: no adapter called colorize.sh,
#     but the stale/running panes above must produce chips within a moment
chips=""
for _ in $(seq 1 15); do
    chips=$(tmux_cmd show-options -wqv -t "$WIN" @agent-panel-chips 2>/dev/null || true)
    [ -n "$chips" ] && break
    sleep 0.2
done
echo "$chips" | grep -q 'colour161' || fail "stale pane should yield a red chip: $chips"
echo "$chips" | grep -q 'colour39' || fail "running pane should yield a blue chip: $chips"
pass "indicator refreshes chips (stale -> red)"

# 4. window scope: only the current window's panes
tmux_cmd new-window -d -t ai -n other
OTHER_PANE="$(tmux_cmd display-message -p -t ai:other.0 '#{pane_id}')"
tmux_cmd set-option -p -t "$OTHER_PANE" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"done\"}"
tmux_cmd set -g @agent-panel-scope window
out=$(run_indicator)
echo "$out" | grep -q '✓' && fail "window scope should not see other window: $out" || true
tmux_cmd set -g @agent-panel-scope session
out=$(run_indicator)
echo "$out" | grep -q '✓1' || fail "session scope should see other window: $out"
pass "window vs session scope"

# 5. bootstrap: status interpolation + window-status formats + old hooks gone
tmux_cmd set -g status-right '#{agent_panel} | %H:%M'
tmux_cmd run-shell "$ROOT_DIR/panel/agent-panel.tmux"
val=$(tmux_cmd show-option -gqv status-right)
echo "$val" | grep -q 'indicator.py' || fail "bootstrap did not interpolate: $val"
if echo "$val" | grep -q '#{agent_panel}'; then
    fail "placeholder not replaced: $val"
fi
wfmt=$(tmux_cmd show-option -gqv window-status-format)
echo "$wfmt" | grep -q '@agent-panel-chips' || fail "window-status-format missing chips: $wfmt"
wcfmt=$(tmux_cmd show-option -gqv window-status-current-format)
echo "$wcfmt" | grep -q '@agent-panel-chips' || fail "current format missing chips: $wcfmt"
hooks=$(tmux_cmd show-hooks -g 2>/dev/null || true)
if echo "$hooks" | grep -q 'pane-focus-in.sh'; then
    fail "old focus hooks not cleaned up"
fi
pass "bootstrap: interpolation + formats + cleanup"

echo "PASS: test-indicator"
