#!/usr/bin/env bash
# Install the tmux-agent-state agent adapters into the hook configs:
#   ~/.claude/settings.json   (claude code)
#   ~/.codex/hooks.json       (codex)
#
# Merges the hooks key: existing hooks of other events are preserved; for our
# events, previous tmux-agent-state entries are replaced (idempotent, no duplicates).
#
#   ./install.sh            both
#   ./install.sh claude     only claude
#   ./install.sh codex      only codex
#
# After installing codex hooks, run /hooks inside codex and trust the
# tmux-agent-state hook definitions before they can run.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_STATE="$REPO_DIR/adapters/agent-state.sh"
CLAUDE_TEMPLATE="$REPO_DIR/adapters/claude-hooks.json"
CODEX_TEMPLATE="$REPO_DIR/adapters/codex-hooks.json"

install_hooks() {  # $1 target-file $2 template-file $3 agent-name
    local target="$1" template="$2" agent="$3"
    mkdir -p "$(dirname "$target")"
    python3 - "$target" "$template" "$AGENT_STATE" "$agent" <<'EOF'
import json, sys

target, template, path, agent = sys.argv[1:5]
try:
    data = json.load(open(target))
except FileNotFoundError:
    data = {}
except ValueError as e:
    # Never silently reset an existing config: a hand-edited file with
    # comments or a trailing comma would be wiped. Refuse and let the user
    # fix or back it up first.
    sys.exit(f"{agent}: {target} exists but is not valid JSON ({e}); refusing to overwrite")
if not isinstance(data, dict):
    sys.exit(f"{agent}: {target} is not a JSON object; refusing to overwrite")

new = json.loads(open(template).read().replace("__AGENT_STATE__", path))
hooks = data.setdefault("hooks", {})
for ev, groups in new["hooks"].items():
    # drop previous tmux-agent-state entries for this event, then append fresh ones
    kept = [g for g in hooks.get(ev, []) if "agent-state.sh" not in json.dumps(g)]
    kept.extend(groups)
    hooks[ev] = kept

json.dump(data, open(target, "w"), indent=2, ensure_ascii=False)
print(f"{agent}: wrote hooks -> {target}")
EOF
}

case "${1:-}" in
    claude)
        install_hooks "$HOME/.claude/settings.json" "$CLAUDE_TEMPLATE" "claude"
        ;;
    codex)
        install_hooks "$HOME/.codex/hooks.json" "$CODEX_TEMPLATE" "codex"
        echo "codex: run /hooks inside codex and trust the tmux-agent-state hooks before they run."
        ;;
    "")
        install_hooks "$HOME/.claude/settings.json" "$CLAUDE_TEMPLATE" "claude"
        install_hooks "$HOME/.codex/hooks.json" "$CODEX_TEMPLATE" "codex"
        echo "codex: run /hooks inside codex and trust the tmux-agent-state hooks before they run."
        ;;
    *)
        echo "usage: install.sh [claude|codex]" >&2
        exit 1
        ;;
esac
