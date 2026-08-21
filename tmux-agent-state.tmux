#!/usr/bin/env bash
# TPM entry point for tmux-agent-state.
#
# TPM run-shells every top-level *.tmux after clone/update (prefix + I / U);
# the real bootstrap lives in statusbar/statusbar.tmux and stays runnable
# standalone for non-TPM installs. The #{agent_status} placeholder contract
# is unchanged: put it in status-right yourself (see README), the bootstrap
# interpolates it in place — same convention as tmux-battery/tmux-cpu.
#
# Only the tmux display layer loads here. Agent-side wiring (install.sh,
# which edits ~/.claude/settings.json etc.) is deliberately NOT part of
# plugin load: run it yourself from the cloned repo.
#
# Idempotent (the bootstrap is), so re-sourcing tmux.conf is safe.

CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
    echo "tmux-agent-state: python3 not found, status segment disabled" >&2
    exit 0
fi

exec "$CURRENT_DIR/statusbar/statusbar.tmux"
