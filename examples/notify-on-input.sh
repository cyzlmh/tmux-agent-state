#!/usr/bin/env bash
# Desktop notification when an agent pane starts waiting for input.
#
# Event-driven, no polling: blocks on the global "agent-state" wait-for
# channel that adapters/agent-state.sh signals on every transition, then
# re-scans all panes (agent_state.py scan). Dedup key is (pane, payload ts):
# signals are edges but scans are levels — a fast busy->asking sequence can
# collapse into one wake, so only the write timestamp reliably identifies a
# new asking episode.
#
#   examples/notify-on-input.sh
#
# Delivery: osascript (macOS) or notify-send (Linux); neither -> the script
# still runs but notifications are dropped. Override the delivery command
# with TMUX_AGENT_STATE_NOTIFY_CMD (called as: $CMD <title> <body>), e.g.:
#   TMUX_AGENT_STATE_NOTIFY_CMD='say' examples/notify-on-input.sh
#
# Panes already asking when the script starts are baselined as seen — no
# startup spam for long-waiting agents. Env: TMUX_STATUS_TMUX overrides the
# tmux command (mainly for tests).

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../statusbar/scripts" && pwd)"
read -r -a TMUX_CMD <<< "${TMUX_STATUS_TMUX:-tmux}"

notify() {  # $1 pane_id
    local title="agent needs input" body="pane $1"
    if [ -n "${TMUX_AGENT_STATE_NOTIFY_CMD:-}" ]; then
        $TMUX_AGENT_STATE_NOTIFY_CMD "$title" "$body" || true
    elif command -v osascript >/dev/null 2>&1; then
        osascript -e "display notification \"$body\" with title \"$title\"" || true
    elif command -v notify-send >/dev/null 2>&1; then
        notify-send "$title" "$body" || true
    fi
}

# space-separated "pane:ts" set, bash 3.2 compatible
seen=""
have_seen() { case " $seen " in *" $1 "*) return 0 ;; *) return 1 ;; esac }

scan_asking() {  # prints "key" per pane currently in needs-input
    python3 "$SCRIPTS_DIR/agent_state.py" scan \
        | awk -F'\t' '$2 == "needs-input" { print $1 ":" $3 }'
}

# baseline: panes already asking count as seen — no startup spam
while IFS= read -r key; do
    [ -n "$key" ] && seen="$seen $key"
done < <(scan_asking)

# wait-for exits non-zero when the server goes away -> the script ends too
while "${TMUX_CMD[@]}" wait-for agent-state 2>/dev/null; do
    asking=""
    while IFS= read -r key; do
        [ -z "$key" ] && continue
        asking="$asking $key"
        if ! have_seen "$key"; then
            seen="$seen $key"
            notify "${key%%:*}"
        fi
    done < <(scan_asking)
    # drop keys whose pane left needs-input: stale keys would grow unbounded
    next=""
    for key in $seen; do
        case " $asking " in
            *" $key "*) next="$next $key" ;;
            *" ${key%%:*}:"*) ;;  # same pane asking again with a newer ts
            *) ;;
        esac
    done
    seen="$next"
done
