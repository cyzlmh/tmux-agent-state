#!/usr/bin/env bash
# Integration tests: window colour chips (panel/scripts/colorize.sh) against
# a real tmux server on an isolated socket.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"

trap cleanup_test_server EXIT
setup_test_server "colorize"

NOW=$(date +%s)
OLD=$((NOW - 100))
COLORIZE="$ROOT_DIR/panel/scripts/colorize.sh"

run_colorize() {
    local pane="${1:-$PANE}"
    tmux_cmd run-shell "TMUX_PANE=$pane \"$COLORIZE\" $pane"
}

get_chips() {
    tmux_cmd show-options -wqv -t "$1" @agent-panel-chips 2>/dev/null || true
}

# 1. single agent pane, done -> one green chip
write_state "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"done\"}"
run_colorize
[ "$(get_chips "$WIN")" = "#[bg=colour34] #[default]" ] \
    || fail "done should give one green chip: $(get_chips "$WIN")"
pass "single done pane -> green chip"

# 2. asking + running panes: chips in layout order (needs-input yellow first,
#    running blue second — split order is top then bottom)
P2="$(tmux_cmd split-window -d -P -F '#{pane_id}' -t ai:main)"
tmux_cmd set-option -p -t "$PANE" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"asking\"}"
tmux_cmd set-option -p -t "$P2" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"busy\",\"ts\":$NOW,\"detail\":\"working\"}"
run_colorize "$P2"
[ "$(get_chips "$WIN")" = "#[bg=colour214] #[default]#[bg=colour39] #[default]" ] \
    || fail "asking+running chips: $(get_chips "$WIN")"
pass "two panes -> two chips in layout order"

# 3. stale -> red chip
tmux_cmd set-option -p -t "$PANE" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$OLD,\"detail\":\"done\"}"
run_colorize "$P2"
[ "$(get_chips "$WIN")" = "#[bg=colour161] #[default]#[bg=colour39] #[default]" ] \
    || fail "stale+running chips: $(get_chips "$WIN")"
pass "stale -> red chip"

# 4. ready panes and non-agent panes get no chip; no agents -> option unset
tmux_cmd set-option -p -t "$PANE" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"ready\"}"
run_colorize "$P2"
[ "$(get_chips "$WIN")" = "#[bg=colour39] #[default]" ] \
    || fail "ready pane should drop its chip: $(get_chips "$WIN")"
tmux_cmd set-option -u -p -t "$P2" @agent-state
run_colorize "$P2"
[ -z "$(get_chips "$WIN")" ] || fail "no agents -> chips unset: $(get_chips "$WIN")"
pass "ready/empty panes dropped"

# 5. layout order respects pane_top (swap states, top pane = PANE2's location)
#    split: PANE is top, P2 is bottom; set top to done, bottom to asking
tmux_cmd set-option -p -t "$PANE" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"done\"}"
tmux_cmd set-option -p -t "$P2" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"asking\"}"
run_colorize "$P2"
[ "$(get_chips "$WIN")" = "#[bg=colour34] #[default]#[bg=colour214] #[default]" ] \
    || fail "layout order top->bottom: $(get_chips "$WIN")"
pass "layout order: top pane first"

# 6. whitespace-tolerant parsing: pretty-printed adapter payloads (spaces
#    after colons) must classify the same as compact JSON
tmux_cmd set-option -u -p -t "$P2" @agent-state
tmux_cmd set-option -p -t "$PANE" @agent-state \
    "{ \"tool\": \"pi\", \"state\": \"waiting\", \"ts\": $NOW, \"detail\": \"done\" }"
run_colorize
[ "$(get_chips "$WIN")" = "#[bg=colour34] #[default]" ] \
    || fail "spaced JSON should parse: $(get_chips "$WIN")"
pass "whitespace-tolerant JSON parsing"

echo "PASS: test-colorize"
