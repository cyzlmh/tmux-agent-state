#!/usr/bin/env bash
# Shared helpers for isolated tmux-socket tests (adapted from tmux-agent-indicator).

set -euo pipefail

TEST_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$TEST_LIB_DIR/../.." && pwd)"

SOCK=""
PANE=""
WIN=""

tmux_cmd() {
    env -u TMUX tmux -L "$SOCK" "$@"
}

cleanup_test_server() {
    if [ -n "${SOCK:-}" ]; then
        tmux_cmd kill-server >/dev/null 2>&1 || true
    fi
}

setup_test_server() {
    local name="${1:-test}"
    SOCK="panel-test-${name}-$$"
    # -f /dev/null: never load the user's real tmux.conf
    tmux_cmd -f /dev/null new-session -d -s ai -n main
    PANE="$(tmux_cmd display-message -p -t ai:main.0 '#{pane_id}')"
    WIN="$(tmux_cmd display-message -p -t ai:main.0 '#{window_id}')"
}

# Write an @agent-state payload to the test pane, exactly like an adapter does.
write_state() {
    local json="$1"
    tmux_cmd set-option -p -t "$PANE" @agent-state "$json"
}

# Render the status segment against the test server (session scope by default).
# CHIPS_REFRESH=0: never throttle, so every run triggers a colorize.sh spawn.
run_indicator() {
    env -u TMUX \
        TMUX_PANEL_TMUX="tmux -L $SOCK" \
        TMUX_PANEL_SESSION="ai" \
        TMUX_PANEL_WINDOW="$WIN" \
        TMUX_PANEL_CHIPS_REFRESH=0 \
        python3 "$REPO_ROOT/panel/scripts/indicator.py"
}

fail() {
    echo "FAIL: $*"
    exit 1
}

pass() {
    echo "PASS: $*"
}

assert_empty() {
    [ -z "$1" ] || fail "$2: $1"
}

assert_non_empty() {
    [ -n "$1" ] || fail "$2"
}
