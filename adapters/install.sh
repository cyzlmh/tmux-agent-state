#!/usr/bin/env bash
# Install the tmux-agent-state agent adapters into the hook configs:
#   ~/.claude/settings.json   (claude code)
#   ~/.codex/hooks.json       (codex)
#   ~/.kimi-code/config.toml  (kimi code)
#
# claude/codex: merges the hooks key — existing hooks of other events are
# preserved; for our events, previous tmux-agent-state entries are replaced
# (idempotent, no duplicates).
# kimi: config.toml is TOML, so the hooks are appended as a marked block;
# re-running replaces the block (idempotent), the rest of the file is kept.
#
#   ./install.sh            all three
#   ./install.sh claude     only claude
#   ./install.sh codex      only codex
#   ./install.sh kimi       only kimi
#
# After installing codex hooks, run /hooks inside codex and trust the
# tmux-agent-state hook definitions before they can run.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_STATE="$REPO_DIR/adapters/agent-state.sh"
CLAUDE_TEMPLATE="$REPO_DIR/adapters/claude-hooks.json"
CODEX_TEMPLATE="$REPO_DIR/adapters/codex-hooks.json"
KIMI_TEMPLATE="$REPO_DIR/adapters/kimi-hooks.toml"

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

install_kimi() {  # $1 target-file $2 template-file
    local target="$1" template="$2"
    mkdir -p "$(dirname "$target")"
    python3 - "$target" "$template" "$AGENT_STATE" <<'EOF'
import sys

target, template, path = sys.argv[1:4]
BEGIN = "# >>> tmux-agent-state >>>"
END = "# <<< tmux-agent-state <<<"

block = BEGIN + "\n" \
    + open(template).read().replace("__AGENT_STATE__", path).strip("\n") \
    + "\n" + END + "\n"

try:
    text = open(target).read()
except FileNotFoundError:
    text = ""

# Drop a previously installed block, keep everything else untouched. Unlike
# the JSON configs we never rewrite the user's TOML, so a hand-edited file
# is safe: appending [[hooks]] entries cannot corrupt the rest.
out, skip = [], False
for line in text.splitlines(keepends=True):
    if line.strip() == BEGIN:
        skip = True
    elif line.strip() == END:
        skip = False
    elif not skip:
        out.append(line)
if skip:
    sys.exit(f"kimi: {target} has an unterminated tmux-agent-state block; fix it manually")
rest = "".join(out).strip("\n")

open(target, "w").write((rest + "\n\n" if rest else "") + block)
print(f"kimi: wrote hooks -> {target}")
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
    kimi)
        install_kimi "$HOME/.kimi-code/config.toml" "$KIMI_TEMPLATE"
        ;;
    "")
        install_hooks "$HOME/.claude/settings.json" "$CLAUDE_TEMPLATE" "claude"
        install_hooks "$HOME/.codex/hooks.json" "$CODEX_TEMPLATE" "codex"
        install_kimi "$HOME/.kimi-code/config.toml" "$KIMI_TEMPLATE"
        echo "codex: run /hooks inside codex and trust the tmux-agent-state hooks before they run."
        ;;
    *)
        echo "usage: install.sh [claude|codex|kimi]" >&2
        exit 1
        ;;
esac
