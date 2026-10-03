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
#   --notify             read the hook JSON from stdin and only report when it
#                        carries a notification_type that means "needs input"
#                        (claude Notification: permission_prompt / idle_prompt /
#                        agent_needs_input / elicitation*). Other notification
#                        types (auth_success, agent_completed, quota_*, …) are
#                        dropped instead of forcing a waiting/asking state.
#                        Combine with --guard on claude, which always uses both.
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
notify=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --agent) agent="${2:-}"; shift 2 ;;
        --state) state="${2:-}"; shift 2 ;;
        --detail) detail="${2:-}"; shift 2 ;;
        --guard) guard=1; shift ;;
        --notify) notify=1; shift ;;
        --adapter-version) shift 2 ;;
        --clear) state=""; shift ;;
        *)
            echo "usage: agent-state.sh --agent <name> --state <waiting|busy> [--detail <hint>] [--guard] [--notify] | --clear" >&2
            exit 1
            ;;
    esac
done

# Hook payload handling. Both flags need the JSON the agent pipes to hooks on
# stdin, so they share one read; the verdict is one line on stdout:
#   drop              subagent event -> never touch the main pane's state
#   notify:<type>     claude Notification -> only needs-input types are reported
#   keep              report as usual (also the fail-open verdict)
# (SubagentStop is not subscribed in claude-hooks.json at all, so it can never
# revive an idle pane. Guard/notify failures fail open.)
if [ "$guard" = 1 ] || [ "$notify" = 1 ]; then
    if [ ! -t 0 ] && command -v python3 >/dev/null 2>&1; then
        # -c (not a heredoc) keeps stdin connected to the hook's payload pipe.
        verdict="$(python3 -c '
import json, select, sys
verdict = "keep"
try:
    if select.select([sys.stdin], [], [], 1.0)[0]:
        data = sys.stdin.read()
        if data.strip():
            payload = json.loads(data)
            if isinstance(payload, dict):
                if payload.get("agent_id"):
                    verdict = "drop"
                elif "notification_type" in payload:
                    verdict = "notify:" + str(payload.get("notification_type"))
except Exception:
    verdict = "keep"
print(verdict)
' 2>/dev/null)" || verdict="keep"
        case "$verdict" in
            drop) exit 0 ;;
            notify:*)
                if [ "$notify" = 1 ]; then
                    case "${verdict#notify:}" in
                        # the only notification types that mean "the agent needs
                        # you"; everything else is informational
                        permission_prompt|idle_prompt|agent_needs_input|\
                        elicitation_dialog|elicitation_url_dialog|\
                        worker_permission_prompt) ;;
                        *) exit 0 ;;
                    esac
                fi
                ;;
        esac
    fi
fi

# no tmux context -> no-op
if [ -z "${TMUX:-}${TMUX_STATUS_TMUX:-}" ] || ! command -v tmux >/dev/null 2>&1; then
    exit 0
fi
read -r -a TMUX_CMD <<< "${TMUX_STATUS_TMUX:-tmux}"

# target pane: $TMUX_PANE from the hook process. If it is missing or dead
# (hooks can fire in child processes where TMUX_PANE is absent or points
# elsewhere, e.g. codex's app-server daemon), recover the pane ourselves —
# but only when the answer is unambiguous; writing a guessed pane's state is
# worse than writing none:
#   1. walk this process's ancestry against pane_pid: the hit is the pane
#      the agent (and therefore this hook) is running in.
#   2. foreground-name scan, but only when exactly one pane matches —
#      several matches means guessing. (Of limited use for agents whose
#      foreground comm is generic: codex shows as "node", claude as its
#      version string, so the walk above is the real fallback there.)
# Never use display-message '#{pane_id}': hooks run detached from any
# client, so it resolves to whatever pane happens to be focused.
# list-panes is the reliable liveness check: display-message -t exits 0
# even for nonexistent panes.
pane="${TMUX_PANE:-}"
if [ -z "$pane" ] || ! "${TMUX_CMD[@]}" list-panes -a -F '#{pane_id}' 2>/dev/null | grep -qx "$pane"; then
    pane=""
    pane_pids=$("${TMUX_CMD[@]}" list-panes -a -F $'#{pane_id}\t#{pane_pid}' 2>/dev/null || true)
    pid="$$"
    while [ -n "$pane_pids" ] && [ "${pid:-1}" -gt 1 ] 2>/dev/null; do
        hit=$(printf '%s\n' "$pane_pids" | awk -F'\t' -v p="$pid" '$2 == p { print $1; exit }')
        if [ -n "$hit" ]; then
            pane="$hit"
            break
        fi
        pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)
    done
    if [ -z "$pane" ] && [ -n "$agent" ]; then
        matches=$("${TMUX_CMD[@]}" list-panes -a -F $'#{pane_id}\t#{pane_current_command}' 2>/dev/null \
            | awk -F'\t' -v a="$agent" 'index($2, a) { print $1 }' || true)
        if [ "$(printf '%s\n' "$matches" | grep -c . || true)" = "1" ]; then
            pane="$matches"
        fi
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

now="$(python3 -c 'import time; print(f"{time.time():.6f}")')"
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
