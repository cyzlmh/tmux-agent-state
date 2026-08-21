#!/usr/bin/env bash
# Integration tests for statusbar/scripts/wait.py: the blocking wait primitive
# driven by the writer's tmux wait-for channel ("agent-state-<pane>").

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"

trap cleanup_test_server EXIT
setup_test_server "wait"
TEST_TMP_OUT="$(mktemp)"
register_tmp_file "$TEST_TMP_OUT"
# A state-bearing pane whose foreground is a shell classifies as stale (rule
# 3); hold a foreground process so the adapter's state is trusted.
hold_pane "$PANE" || fail "hold pane"

run_agent_state() {
    TMUX_STATUS_TMUX="tmux -L $SOCK" TMUX_PANE="$PANE" \
        TMUX_AGENT_STATE_LOG= \
        bash "$ROOT_DIR/adapters/agent-state.sh" "$@"
}

run_wait() {
    TMUX_STATUS_TMUX="tmux -L $SOCK" python3 "$ROOT_DIR/statusbar/scripts/wait.py" "$@"
}

# 1. already in a wanted state -> exits immediately
run_agent_state --agent claude --state busy --detail working
out=$(run_wait "$PANE" running --timeout 5)
[ "$out" = "running" ] || fail "immediate match should print 'running': $out"
pass "immediate match"

# 2. blocks until the writer signals the channel
run_agent_state --agent claude --state waiting --detail done
run_wait "$PANE" needs-input --timeout 10 >"$TEST_TMP_OUT" 2>&1 &
waiter=$!
sleep 0.5
kill -0 "$waiter" 2>/dev/null || fail "wait.py should still be blocked: $(cat "$TEST_TMP_OUT")"
run_agent_state --agent claude --state waiting --detail asking
rc=0
wait "$waiter" || rc=$?
[ "$rc" = 0 ] || fail "wait.py should exit 0 after the transition (rc=$rc)"
[ "$(cat "$TEST_TMP_OUT")" = "needs-input" ] || fail "output: $(cat "$TEST_TMP_OUT")"
pass "blocks until transition"

# 3. any of several wanted states matches
run_agent_state --agent claude --state waiting --detail done
out=$(run_wait "$PANE" needs-input done --timeout 5)
[ "$out" = "done" ] || fail "multi-state match: $out"
pass "multiple wanted states"

# 4. timeout exits 1
if run_wait "$PANE" running --timeout 1 >/dev/null 2>&1; then
    fail "wait.py should time out while state is done"
fi
pass "timeout exits 1"

# 5. unknown pane exits 1
if run_wait "%999999" running --timeout 1 >/dev/null 2>&1; then
    fail "wait.py should fail on a nonexistent pane"
fi
pass "nonexistent pane exits 1"

echo "PASS: test-wait"
