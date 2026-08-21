#!/usr/bin/env bash
# Smoke test for the TPM entry point (tmux-agent-state.tmux), against a real
# tmux server on an isolated socket: it loads the bootstrap, interpolates the
# #{agent_status} placeholder in place, is idempotent, and leaves options
# without the placeholder untouched.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"

trap cleanup_test_server EXIT
setup_test_server "tpm-entry"

# The entry calls plain `tmux`; shim it onto the isolated socket via PATH.
# Resolve the real binary first — a shim that execs `tmux` by name would
# re-resolve to itself (SHIM_DIR is first in PATH) and recurse forever.
REAL_TMUX="$(command -v tmux)"
SHIM_DIR="$(mktemp -d)"
register_tmp_file "$SHIM_DIR"
cat > "$SHIM_DIR/tmux" <<EOF
#!/usr/bin/env bash
exec "$REAL_TMUX" -L "$SOCK" "\$@"
EOF
chmod +x "$SHIM_DIR/tmux"

run_entry() {
    PATH="$SHIM_DIR:$PATH" bash "$ROOT_DIR/tmux-agent-state.tmux"
}

# 1. no placeholder anywhere -> exit 0, status-right untouched
before="$(tmux_cmd show-option -gqv status-right)"
run_entry || fail "entry should exit 0 without a placeholder"
after="$(tmux_cmd show-option -gqv status-right)"
[ "$before" = "$after" ] || fail "status-right without placeholder must stay untouched: $after"
pass "no placeholder -> untouched"

# 2. placeholder present -> interpolated, surrounding segments preserved
tmux_cmd set-option -gq status-right '#{agent_status} | %H:%M'
run_entry || fail "entry should exit 0 with placeholder"
v1="$(tmux_cmd show-option -gqv status-right)"
case "$v1" in
    *indicator.py*) : ;;
    *) fail "placeholder not interpolated: $v1" ;;
esac
case "$v1" in
    *' | %H:%M') : ;;
    *) fail "trailing segment lost during interpolation: $v1" ;;
esac
pass "placeholder interpolated in place"

# 3. second run -> identical (TPM re-runs the entry on every conf reload)
run_entry || fail "second run should exit 0"
v2="$(tmux_cmd show-option -gqv status-right)"
[ "$v1" = "$v2" ] || fail "second run changed status-right: $v2"
pass "idempotent"

echo "PASS: tpm entry"
