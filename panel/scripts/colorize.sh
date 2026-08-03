#!/usr/bin/env bash
# tmux-agent-state window chips: one colour chip per agent pane in a window.
#
# A consumer of the @agent-state pane option (see PROTOCOL.md). It computes,
# per window, a sequence of colour chips — one per agent pane, in pane layout
# order (top-to-bottom, left-to-right) — and writes it to the window user
# option @agent-panel-chips:
#
#   #[bg=colour34] #[default]#[bg=colour214] #[default]
#
# The window-status format (set up by panel/agent-panel.tmux) renders these
# chips right after the window name, so a window label shows exactly how many
# agents it holds and what state each is in:
#
#   [0] train ▮▮▮       3 agent panes: done / asking / done
#
# Chip colours are the per-state @agent-panel-color-* options (same set as
# panel/scripts/indicator.py). running also gets a chip (it is information,
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
# Test hooks (env): TMUX_PANEL_TMUX overrides the tmux command, e.g.
# "tmux -L testsocket" for an isolated server.

set -euo pipefail

# No tmux context -> no-op. Server jobs (run-shell / #()) always get $TMUX.
[ -n "${TMUX:-}${TMUX_PANEL_TMUX:-}" ] || exit 0

read -r -a TMUX_CMD <<< "${TMUX_PANEL_TMUX:-tmux}"
command -v "${TMUX_CMD[0]}" >/dev/null 2>&1 || exit 0

STALE=45

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
read_state() {
    local raw
    raw=$("${TMUX_CMD[@]}" show-options -pqv -t "$1" @agent-state 2>/dev/null || true)
    DS=""
    [ -n "$raw" ] || return 0
    DS=$(AGENT_STATE="$raw" STALE="$STALE" python3 -c '
import json, os, time
try:
    d = json.loads(os.environ["AGENT_STATE"])
    st, ts = d.get("state"), float(d.get("ts", 0))
except (ValueError, TypeError):
    raise SystemExit(0)
if st not in ("busy", "waiting") or ts <= 0:
    raise SystemExit(0)
if time.time() - ts > float(os.environ["STALE"]):
    print("stale")           # stale wins over any reported state (PROTOCOL.md)
elif st == "busy":
    print("running")
elif d.get("detail") == "asking":
    print("needs-input")
elif d.get("detail") == "done":
    print("done")
' 2>/dev/null) || DS=""
}

color_for() {
    case "$1" in
        needs-input) tmux_get_option_or_default "@agent-panel-color-needs-input" "colour214" ;;
        done) tmux_get_option_or_default "@agent-panel-color-done" "colour34" ;;
        stale) tmux_get_option_or_default "@agent-panel-color-stale" "colour161" ;;
        running) tmux_get_option_or_default "@agent-panel-color-running" "colour39" ;;
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
    "${TMUX_CMD[@]}" set-option -wq -t "$window_id" @agent-panel-chips "$chips"
else
    "${TMUX_CMD[@]}" set-option -wq -u -t "$window_id" @agent-panel-chips
fi

"${TMUX_CMD[@]}" refresh-client -S >/dev/null 2>&1 || true
