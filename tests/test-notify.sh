#!/usr/bin/env bash
# Integration test for examples/notify-on-input.sh: event-driven desktop
# notifications on transitions into needs-input, deduped per asking episode.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"

trap cleanup_test_server EXIT
setup_test_server "notify"
hold_pane "$PANE" || fail "hold pane"

NOTIFY_OUT="$(mktemp)"
register_tmp_file "$NOTIFY_OUT"
RECORDER="$(mktemp)"
register_tmp_file "$RECORDER"
cat > "$RECORDER" <<'EOS'
#!/bin/sh
echo "$2" >> "$NOTIFY_OUT"
EOS
chmod +x "$RECORDER"

run_agent_state() {
    TMUX_STATUS_TMUX="tmux -L $SOCK" TMUX_PANE="$PANE" \
        TMUX_AGENT_STATE_LOG= \
        bash "$ROOT_DIR/adapters/agent-state.sh" "$@"
}

wait_lines() {  # $1 expected line count
    for _ in $(seq 1 100); do
        [ "$(wc -l < "$NOTIFY_OUT" | tr -d ' ')" = "$1" ] && return 0
        sleep 0.05
    done
    return 1
}

TMUX_STATUS_TMUX="tmux -L $SOCK" \
    TMUX_AGENT_STATE_NOTIFY_CMD="sh $RECORDER" \
    NOTIFY_OUT="$NOTIFY_OUT" \
    bash "$ROOT_DIR/examples/notify-on-input.sh" &
NOTIFIER=$!
sleep 0.5
kill -0 "$NOTIFIER" 2>/dev/null || fail "notifier should be running"

# 1. transition into asking -> exactly one notification
run_agent_state --agent claude --state busy --detail working
run_agent_state --agent claude --state waiting --detail asking
wait_lines 1 || fail "expected 1 notification: $(cat "$NOTIFY_OUT")"
grep -q "pane $PANE" "$NOTIFY_OUT" || fail "notification body: $(cat "$NOTIFY_OUT")"
pass "notifies on asking"

# 2. a spurious channel signal while still asking (no new write) -> no dup
tmux_cmd wait-for -S agent-state
sleep 0.3
[ "$(wc -l < "$NOTIFY_OUT" | tr -d ' ')" = "1" ] \
    || fail "spurious signal should not re-notify: $(cat "$NOTIFY_OUT")"
pass "spurious signal deduped"

# 3. leave asking, come back -> notified again
run_agent_state --agent claude --state busy --detail working
run_agent_state --agent claude --state waiting --detail asking
wait_lines 2 || fail "expected a second notification: $(cat "$NOTIFY_OUT")"
pass "new episode notifies again"

kill "$NOTIFIER" 2>/dev/null || true
wait "$NOTIFIER" 2>/dev/null || true

echo "PASS: test-notify"
