#!/usr/bin/env bash
# tmux-agent-state bootstrap: interpolate #{agent_status} into the status bar
# and set up the window-status formats that render per-agent colour chips.
#
# Usage: add '#{agent_status}' to a status option, then load once per server:
#
#   set -g status-right '#{agent_status} | %H:%M'
#   tmux run-shell "$HOME/tmux-agent-state/statusbar/statusbar.tmux"
#
# (or add the run-shell line to ~/.tmux.conf). Idempotent: the placeholder
# is replaced on first load, so re-running does not double-interpolate.
#
# window-status-format / window-status-current-format are only set when the
# user has not customised them; the default is extended with
# #{@agent-status-chips} (one colour chip per agent pane, see colorize.sh).

CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INDICATOR="$CURRENT_DIR/scripts/indicator.py"

interpolate() {
    local string="$1"
    string="${string//\#\{agent_status\}/#(python3 \"$INDICATOR\")}"
    echo "$string"
}

update_option() {
    local option="$1"
    local value
    value=$(tmux show-option -gqv "$option")
    if [ -z "$value" ]; then
        return
    fi
    tmux set-option -gq "$option" "$(interpolate "$value")"
}

# Extend the window-label formats with the chips segment. Idempotent: if
# the format already carries #{@agent-status-chips}, leave it untouched;
# otherwise append (preserving any user-customised format).
ensure_window_status_formats() {
    local opt v
    for opt in window-status-format window-status-current-format; do
        v=$(tmux show-option -gqv "$opt")
        if [[ "$v" != *"@agent-status-chips"* ]]; then
            tmux set-option -gq "$opt" "${v}#{@agent-status-chips}"
        fi
    done
}

# Remove hooks registered by older versions of this bootstrap (focus-based
# style clearing is gone — chips are pure information).
cleanup_old_hooks() {
    local existing line name
    existing=$(tmux show-hooks -g 2>/dev/null || true)
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        case "$line" in
            *pane-focus-in.sh*)
                name="${line%% *}"
                tmux set-hook -gu "$name" 2>/dev/null || true
                ;;
        esac
    done < <(printf '%s\n' "$existing")
}

main() {
    update_option "status-right"
    update_option "status-left"
    update_option "@minimal-tmux-status-right"
    update_option "@minimal-tmux-status-left"
    update_option "@minimal-tmux-status-right-extra"
    update_option "@minimal-tmux-status-left-extra"
    ensure_window_status_formats
    cleanup_old_hooks
}

main
