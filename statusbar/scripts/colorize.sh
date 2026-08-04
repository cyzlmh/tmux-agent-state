#!/usr/bin/env bash
# tmux-agent-state window chips: one colour chip per agent pane in a window.
#
# A consumer of the @agent-state pane option (see PROTOCOL.md). It computes,
# per window, a sequence of colour chips — one per agent pane, in pane layout
# order (top-to-bottom, left-to-right) — and writes it to the window user
# option @agent-status-chips:
#
#   #[bg=colour34] #[default]#[bg=colour214] #[default]
#
# The window-status format (set up by statusbar/statusbar.tmux) renders these
# chips right after the window name, so a window label shows exactly how many
# agents it holds and what state each is in:
#
#   [0] train ▮▮▮       3 agent panes: done / asking / done
#
# Chip colours are the per-state @agent-status-color-* options (same set as
# statusbar/scripts/indicator.py). running also gets a chip (it is information,
# not an alert). No window/global styling is touched: chips are pure
# information, so nothing needs focus-based reset.
#
# Invocation:
#
#   colorize.sh [pane_id]    refresh chips for pane's window
#
# Called by adapters after state transitions (the pi extension; claude/codex
# hooks will too), and periodically by indicator.py on status-bar redraws —
# staleness and adapter shutdown (option cleared) are not transitions, so
# without the periodic trigger a dead agent's chip would keep its last colour
# forever.
#
# Test hooks (env): TMUX_STATUS_TMUX overrides the tmux command, e.g.
# "tmux -L testsocket" for an isolated server.

set -euo pipefail

# No tmux context -> no-op. Server jobs (run-shell / #()) always get $TMUX.
[ -n "${TMUX:-}${TMUX_STATUS_TMUX:-}" ] || exit 0

read -r -a TMUX_CMD <<< "${TMUX_STATUS_TMUX:-tmux}"
command -v "${TMUX_CMD[0]}" >/dev/null 2>&1 || exit 0

tmux_get_option_or_default() {
    if [ -n "$("${TMUX_CMD[@]}" show-option -gq "$1" 2>/dev/null || true)" ]; then
        "${TMUX_CMD[@]}" show-option -gqv "$1"
    else
        printf '%s\n' "$2"
    fi
}

# read_state <pane>: sets DS (display state: needs-input|done|stale|running|"")
# The payload is parsed with python3 (same JSON parser as indicator.py), so
# any conforming adapter payload works regardless of key order or whitespace.
# stale is decided by process presence, not ts: with no heartbeat, a pane
# whose foreground is a shell no longer runs its adapter (PROTOCOL.md).
read_state() {
    local raw cmd
    raw=$("${TMUX_CMD[@]}" show-options -pqv -t "$1" @agent-state 2>/dev/null || true)
    DS=""
    [ -n "$raw" ] || return 0
    cmd=$("${TMUX_CMD[@]}" display-message -p -t "$1" '#{pane_current_command}' 2>/dev/null || true)
    case "${cmd#-}" in
        zsh|bash|fish|sh|dash|tcsh|ksh|nu)
            DS="stale"   # adapter process gone
            return 0
            ;;
    esac
    DS=$(AGENT_STATE="$raw" python3 -c '
import json, os
try:
    d = json.loads(os.environ["AGENT_STATE"])
    st = d.get("state")
except (ValueError, TypeError):
    raise SystemExit(0)
if st == "busy":
    print("running")
elif st == "waiting" and d.get("detail") == "asking":
    print("needs-input")
elif st == "waiting" and d.get("detail") == "done":
    print("done")
' 2>/dev/null) || DS=""
}

color_for() {
    case "$1" in
        needs-input) tmux_get_option_or_default "@agent-status-color-needs-input" "colour214" ;;
        done) tmux_get_option_or_default "@agent-status-color-done" "colour34" ;;
        stale) tmux_get_option_or_default "@agent-status-color-stale" "colour161" ;;
        running) tmux_get_option_or_default "@agent-status-color-running" "colour39" ;;
        *) printf '' ;;
    esac
}

pane="${1:-${TMUX_PANE:-}}"
if [ -z "$pane" ]; then
    pane=$("${TMUX_CMD[@]}" display-message -p '#{pane_id}' 2>/dev/null || true)
fi
[ -n "$pane" ] || exit 0

window_id=$("${TMUX_CMD[@]}" display-message -p -t "$pane" '#{window_id}')

# panes in layout order: top-to-bottom, then left-to-right
chips=""
while IFS=' ' read -r _ _ pane_id; do
    [ -n "$pane_id" ] || continue
    read_state "$pane_id"
    [ -n "$DS" ] || continue
    color=$(color_for "$DS")
    [ -n "$color" ] || continue
    chips="${chips}#[bg=${color}] #[default]"
done < <("${TMUX_CMD[@]}" list-panes -t "$window_id" -F '#{pane_top} #{pane_left} #{pane_id}' | sort -n -k1,1 -k2,2)

if [ -n "$chips" ]; then
    "${TMUX_CMD[@]}" set-option -wq -t "$window_id" @agent-status-chips "$chips"
else
    "${TMUX_CMD[@]}" set-option -wq -u -t "$window_id" @agent-status-chips
fi

"${TMUX_CMD[@]}" refresh-client -S >/dev/null 2>&1 || true
