#!/usr/bin/env bash
# tmux-agent-state adapter for hook-based agents (claude code / codex).
#
# Writes the @agent-state pane option from agent lifecycle events, exactly
# like adapters/pi/agent-state.ts but invoked from shell hooks (see claude-hooks.json
# / codex-hooks.json; install.sh wires them up). No heartbeat (PROTOCOL.md):
# the reader decides liveness from the pane foreground command, so hooks only
# write on real state transitions.
#
# Usage:
#   agent-state.sh --agent claude --state waiting --detail ready
#   agent-state.sh --agent claude --state busy    --detail working
#   agent-state.sh --agent claude --state waiting --detail asking
#   agent-state.sh --agent claude --state waiting --detail done
#   agent-state.sh --clear
#
# No-op when not inside tmux. Env: TMUX_STATUS_TMUX overrides the tmux
# command (e.g. "tmux -L testsocket"), mainly for tests.

set -euo pipefail

agent=""
state=""
detail=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --agent) agent="${2:-}"; shift 2 ;;
        --state) state="${2:-}"; shift 2 ;;
        --detail) detail="${2:-}"; shift 2 ;;
        --clear) state=""; shift ;;
        *)
            echo "usage: agent-state.sh --agent <name> --state <waiting|busy> [--detail <hint>] | --clear" >&2
            exit 1
            ;;
    esac
done

# no tmux context -> no-op
if [ -z "${TMUX:-}${TMUX_STATUS_TMUX:-}" ] || ! command -v tmux >/dev/null 2>&1; then
    exit 0
fi
read -r -a TMUX_CMD <<< "${TMUX_STATUS_TMUX:-tmux}"

# target pane: $TMUX_PANE from the hook process; if it is missing or dead,
# scan for a pane whose foreground runs this agent (hooks can fire in child
# processes where TMUX_PANE points elsewhere). list-panes is the reliable
# liveness check: display-message -t exits 0 even for nonexistent panes.
pane="${TMUX_PANE:-}"
if [ -z "$pane" ] || ! "${TMUX_CMD[@]}" list-panes -a -F '#{pane_id}' 2>/dev/null | grep -qx "$pane"; then
    pane=""
    if [ -n "$agent" ]; then
        pane=$("${TMUX_CMD[@]}" list-panes -a -F $'#{pane_id}\t#{pane_current_command}' 2>/dev/null \
            | awk -F'\t' -v a="$agent" 'index($2, a) { print $1; exit }')
    fi
    if [ -z "$pane" ]; then
        pane=$("${TMUX_CMD[@]}" display-message -p '#{pane_id}' 2>/dev/null || true)
    fi
fi
[ -n "$pane" ] || exit 0

if [ -z "$state" ]; then
    "${TMUX_CMD[@]}" set-option -u -p -t "$pane" @agent-state
    exit 0
fi

case "$state" in
    waiting|busy) ;;
    *) echo "agent-state.sh: bad state '$state' (waiting|busy)" >&2; exit 1 ;;
esac

payload=$(printf '{"tool":"%s","state":"%s","ts":%s,"detail":"%s"}' \
    "$agent" "$state" "$(date +%s)" "$detail")
"${TMUX_CMD[@]}" set-option -p -t "$pane" @agent-state "$payload"
