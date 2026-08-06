#!/usr/bin/env bash
# Integration tests: window colour chips (statusbar/scripts/colorize.sh) against
# a real tmux server on an isolated socket.
#
# stale is decided by process presence (no-heartbeat protocol): a pane whose
# foreground command is a shell counts as stale even with a fresh payload.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"

trap cleanup_test_server EXIT
setup_test_server "colorize"

NOW=$(date +%s)
COLORIZE="$ROOT_DIR/statusbar/scripts/colorize.sh"

run_colorize() {
    local pane="${1:-$PANE}"
    tmux_cmd run-shell "TMUX_PANE=$pane \"$COLORIZE\" $pane"
}

get_chips() {
    tmux_cmd show-options -wqv -t "$1" @agent-status-chips 2>/dev/null || true
}

# 1. state on a shell foreground -> stale chip (soft red), no heartbeat
write_state "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"done\"}"
run_colorize
[ "$(get_chips "$WIN")" = "#[bg=colour167] #[default]" ] \
    || fail "shell pane with done state should be stale (soft red): $(get_chips "$WIN")"
pass "shell foreground -> stale chip"

# 2. live process -> done chip (sage)
hold_pane "$PANE" || fail "could not hold pane"
run_colorize
[ "$(get_chips "$WIN")" = "#[bg=colour108] #[default]" ] \
    || fail "live process done -> sage chip: $(get_chips "$WIN")"
pass "live process done -> sage chip"

# 3. two live panes: asking + running -> sand + steel blue, in layout order
P2="$(tmux_cmd split-window -d -P -F '#{pane_id}' -t ai:main)"
hold_pane "$P2" || fail "hold P2"
tmux_cmd set-option -p -t "$PANE" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"asking\"}"
tmux_cmd set-option -p -t "$P2" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"busy\",\"ts\":$NOW,\"detail\":\"working\"}"
run_colorize "$P2"
[ "$(get_chips "$WIN")" = "#[bg=colour180] #[default]#[bg=colour68] #[default]" ] \
    || fail "asking+running chips: $(get_chips "$WIN")"
pass "two panes -> sand + blue chips"

# 4. ready panes get no chip; no agents -> option unset
tmux_cmd set-option -p -t "$PANE" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"ready\"}"
run_colorize "$P2"
[ "$(get_chips "$WIN")" = "#[bg=colour68] #[default]" ] \
    || fail "ready pane should drop its chip: $(get_chips "$WIN")"
tmux_cmd set-option -u -p -t "$P2" @agent-state
run_colorize "$P2"
[ -z "$(get_chips "$WIN")" ] || fail "no agents -> chips unset: $(get_chips "$WIN")"
pass "ready/empty panes dropped"

# 5. layout order respects pane_top (top pane first)
tmux_cmd set-option -p -t "$PANE" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"done\"}"
tmux_cmd set-option -p -t "$P2" @agent-state \
    "{\"tool\":\"pi\",\"state\":\"waiting\",\"ts\":$NOW,\"detail\":\"asking\"}"
run_colorize "$P2"
[ "$(get_chips "$WIN")" = "#[bg=colour108] #[default]#[bg=colour180] #[default]" ] \
    || fail "layout order top->bottom: $(get_chips "$WIN")"
pass "layout order: top pane first"

echo "PASS: test-colorize"
