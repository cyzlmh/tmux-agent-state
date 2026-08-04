#!/usr/bin/env bash
# Shared helpers for isolated tmux-socket tests (adapted from tmux-agent-indicator).

set -euo pipefail

TEST_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$TEST_LIB_DIR/../.." && pwd)"

SOCK=""
PANE=""
WIN=""
TEST_TMP_FILES=()

register_tmp_file() {
    TEST_TMP_FILES+=("$1")
}

tmux_cmd() {
    env -u TMUX tmux -L "$SOCK" "$@"
}

cleanup_test_server() {
    if [ -n "${SOCK:-}" ]; then
        tmux_cmd kill-server >/dev/null 2>&1 || true
    fi
    for path in "${TEST_TMP_FILES[@]:-}"; do
        rm -rf "$path" >/dev/null 2>&1 || true
    done
}

setup_test_server() {
    local name="${1:-test}"
    SOCK="statusbar-test-${name}-$$"
    # -f /dev/null: never load the user's real tmux.conf. The pane shell is
    # zsh -f too: a login shell would run the user's .zshrc (brew, git,
    # docker checks…) whose child processes intermittently take over the
    # foreground, making pane_current_command flaky for the process-presence
    # assertions.
    tmux_cmd -f /dev/null new-session -d -s ai -n main "/bin/zsh -f"
    PANE="$(tmux_cmd display-message -p -t ai:main.0 '#{pane_id}')"
    WIN="$(tmux_cmd display-message -p -t ai:main.0 '#{window_id}')"
    # Right after new-session the initial shell is still starting and
    # pane_current_command can briefly be empty; wait until it settles so the
    # process-presence tests see a stable foreground.
    for _ in $(seq 1 40); do
        cmd=$(tmux_cmd display-message -p -t "$PANE" '#{pane_current_command}' 2>/dev/null || true)
        case "$cmd" in
            ""|login) sleep 0.05 ;;
            *) break ;;
        esac
    done
}

# Write an @agent-state payload to the test pane, exactly like an adapter does.
write_state() {
    local json="$1"
    tmux_cmd set-option -p -t "$PANE" @agent-state "$json"
}

# Run a foreground process in a pane so pane_current_command is not a shell.
# Without this, a state-bearing pane is classified stale (no-heartbeat rule).
hold_pane() {
    local pane="$1"
    tmux_cmd send-keys -t "$pane" "sleep 3600" Enter
    # 5s budget: the keys are buffered in the pty, so a slow shell startup
    # (loaded machine) just takes longer to reach the sleep foreground; the
    # old 1s budget flaked under load.
    for _ in $(seq 1 100); do
        if [ "$(tmux_cmd display-message -p -t "$pane" '#{pane_current_command}' 2>/dev/null || true)" = "sleep" ]; then
            return 0
        fi
        sleep 0.05
    done
    return 1
}

# Render the status segment against the test server (session scope by default).
# CHIPS_REFRESH=0: never throttle, so every run triggers a colorize.sh spawn.
run_indicator() {
    env -u TMUX \
        TMUX_STATUS_TMUX="tmux -L $SOCK" \
        TMUX_STATUS_SESSION="ai" \
        TMUX_STATUS_WINDOW="$WIN" \
        TMUX_STATUS_CHIPS_REFRESH=0 \
        python3 "$REPO_ROOT/statusbar/scripts/indicator.py"
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
