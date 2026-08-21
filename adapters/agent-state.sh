#!/usr/bin/env bash
# tmux-agent-state adapter for hook-based agents (claude code / codex / kimi code).
#
# Writes the @agent-state pane option from agent lifecycle events, exactly
# like adapters/pi/agent-state.ts but invoked from shell hooks (see claude-hooks.json
# / codex-hooks.json / kimi-hooks.toml; install.sh wires them up). No heartbeat (PROTOCOL.md):
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
# Flags:
#   --guard              read the hook JSON from stdin (non-tty only) and drop
#                        subagent events (payload with agent_id, e.g. claude
#                        Task subagents) instead of letting them overwrite the
#                        main pane's state. Fail-open: unreadable/unknown
#                        payloads are reported as usual.
#   --adapter-version N  ignored marker; lets install.sh --check report which
#                        template version is installed.
#
# Side effects on every write/clear (best effort, never fail the hook):
#   - signals the tmux wait-for channel "agent-state-<pane>" (see wait.py)
#   - appends one JSONL line to $TMUX_AGENT_STATE_LOG
#     (default ${TMPDIR:-/tmp}/tmux-agent-state.log; empty disables)
#
# No-op when not inside tmux. Env: TMUX_STATUS_TMUX overrides the tmux
# command (e.g. "tmux -L testsocket"), mainly for tests.

set -euo pipefail

agent=""
state=""
detail=""
guard=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --agent) agent="${2:-}"; shift 2 ;;
        --state) state="${2:-}"; shift 2 ;;
        --detail) detail="${2:-}"; shift 2 ;;
        --guard) guard=1; shift ;;
        --adapter-version) shift 2 ;;
        --clear) state=""; shift ;;
        *)
            echo "usage: agent-state.sh --agent <name> --state <waiting|busy> [--detail <hint>] [--guard] | --clear" >&2
            exit 1
            ;;
    esac
done

# Subagent guard: hook payloads carrying agent_id belong to a subagent (e.g.
# claude Task agents); their tool/stop events must not touch the main pane's
# state. (SubagentStop is not subscribed in claude-hooks.json at all, so it
# can never revive an idle pane.) Guard failures fail open.
if [ "$guard" = 1 ] && [ ! -t 0 ] && command -v python3 >/dev/null 2>&1; then
    # -c (not a heredoc) keeps stdin connected to the hook's payload pipe.
    rc=0
    python3 -c '
import json, select, sys
drop = 0
try:
    if select.select([sys.stdin], [], [], 1.0)[0]:
        data = sys.stdin.read()
        if data.strip():
            payload = json.loads(data)
            if isinstance(payload, dict) and payload.get("agent_id"):
                drop = 10
except Exception:
    drop = 0
sys.exit(drop)
' || rc=$?
    [ "$rc" = 10 ] && exit 0
fi

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

# Best-effort side effects shared by write and clear: wake wait.py waiters
# blocked on this pane's channel (plus the global "agent-state" channel used
# by examples/notify-on-input.sh), and append to the JSONL transition log.
notify() {  # $1 = logged state ("cleared" on --clear)
    "${TMUX_CMD[@]}" wait-for -S "agent-state-$pane" 2>/dev/null || true
    "${TMUX_CMD[@]}" wait-for -S "agent-state" 2>/dev/null || true
    local log="${TMUX_AGENT_STATE_LOG-${TMPDIR:-/tmp}/tmux-agent-state.log}"
    [ -n "$log" ] || return 0
    printf '{"ts":%s,"pane":"%s","tool":"%s","state":"%s","detail":"%s"}\n' \
        "$(date +%s)" "$pane" "$agent" "$1" "$detail" >> "$log" 2>/dev/null || true
}

if [ -z "$state" ]; then
    "${TMUX_CMD[@]}" set-option -u -p -t "$pane" @agent-state
    notify cleared
    exit 0
fi

case "$state" in
    waiting|busy) ;;
    *) echo "agent-state.sh: bad state '$state' (waiting|busy)" >&2; exit 1 ;;
esac

now="$(date +%s)"
payload=$(printf '{"tool":"%s","state":"%s","ts":%s,"detail":"%s"}' \
    "$agent" "$state" "$now" "$detail")
"${TMUX_CMD[@]}" set-option -p -t "$pane" @agent-state "$payload"
notify "$state"

# Refresh window-label chips, same contract as the pi adapter: hooks write
# outside pi's transitions, so agent-state.ts's colourize() never runs for
# them — without this the chip stays on its last colour until indicator.py's
# periodic refresh. TMUX_STATUS_COLORIZE overrides the path; empty disables.
COLORIZE="${TMUX_STATUS_COLORIZE-$(dirname "$0")/../statusbar/scripts/colorize.sh}"
if [ -n "$COLORIZE" ] && [ -x "$COLORIZE" ]; then
    "$COLORIZE" "$pane" >/dev/null 2>&1 &
fi
