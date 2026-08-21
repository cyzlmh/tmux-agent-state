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
#   ./install.sh --check [claude|codex|kimi]
#       report whether the installed wiring matches the current templates
#       (no writes; exit 1 when anything is missing or outdated — re-run
#       install.sh to upgrade)
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

check_hooks() {  # $1 target-file $2 template-file $3 agent-name
    local target="$1" template="$2" agent="$3"
    python3 - "$target" "$template" "$AGENT_STATE" "$agent" <<'EOF'
import json, re, sys

target, template, path, agent = sys.argv[1:5]

def version(text):
    found = re.findall(r"--adapter-version (\d+)", text)
    return f"v{max(map(int, found))}" if found else "unversioned"

template_text = open(template).read()
tmpl_ver = version(template_text)
try:
    data = json.load(open(target))
except FileNotFoundError:
    print(f"{agent}: NOT INSTALLED ({target} missing; template is {tmpl_ver})")
    sys.exit(1)
except ValueError as e:
    # Same rule as install: never touch a config we cannot parse.
    print(f"{agent}: cannot check, {target} is not valid JSON ({e})")
    sys.exit(1)
if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
    print(f"{agent}: cannot check, {target} has no hooks object")
    sys.exit(1)

new = json.loads(template_text.replace("__AGENT_STATE__", path))
hooks = data.get("hooks", {})
drift = []
installed_text = ""
for ev, groups in new["hooks"].items():
    installed = [g for g in hooks.get(ev, []) if "agent-state.sh" in json.dumps(g)]
    installed_text += json.dumps(installed)
    if installed != groups:
        drift.append(ev)
for ev, groups in hooks.items():
    if ev not in new["hooks"] and any("agent-state.sh" in json.dumps(g) for g in groups):
        drift.append(f"{ev} (no longer in template)")
if drift:
    print(f"{agent}: OUTDATED (installed {version(installed_text)} -> template {tmpl_ver}; "
          f"drifted: {', '.join(drift)}) — re-run install.sh {agent}")
    sys.exit(1)
print(f"{agent}: up to date (adapter {tmpl_ver})")
EOF
}

check_kimi() {  # $1 target-file $2 template-file
    local target="$1" template="$2"
    python3 - "$target" "$template" "$AGENT_STATE" <<'EOF'
import re, sys

target, template, path = sys.argv[1:4]
BEGIN = "# >>> tmux-agent-state >>>"
END = "# <<< tmux-agent-state <<<"

def version(text):
    found = re.findall(r"--adapter-version (\d+)", text)
    return f"v{max(map(int, found))}" if found else "unversioned"

expected = open(template).read().replace("__AGENT_STATE__", path).strip("\n")
tmpl_ver = version(expected)
try:
    text = open(target).read()
except FileNotFoundError:
    print(f"kimi: NOT INSTALLED ({target} missing; template is {tmpl_ver})")
    sys.exit(1)

installed, inside, terminated = [], False, False
for line in text.splitlines():
    if line.strip() == BEGIN:
        inside = True
    elif line.strip() == END:
        inside, terminated = False, True
    elif inside:
        installed.append(line)
if inside:
    print(f"kimi: cannot check, {target} has an unterminated tmux-agent-state block")
    sys.exit(1)
if not terminated:
    print(f"kimi: NOT INSTALLED (no tmux-agent-state block in {target}; template is {tmpl_ver})")
    sys.exit(1)
installed_text = "\n".join(installed).strip("\n")
if installed_text != expected:
    print(f"kimi: OUTDATED (installed {version(installed_text)} -> template {tmpl_ver})"
          " — re-run install.sh kimi")
    sys.exit(1)
print(f"kimi: up to date (adapter {tmpl_ver})")
EOF
}

CHECK=0
if [ "${1:-}" = "--check" ]; then
    CHECK=1
    shift
fi

status=0
step() {  # $1 agent-name; check mode reports drift, install mode rewrites
    if [ "$CHECK" = 1 ]; then
        case "$1" in
            claude) check_hooks "$HOME/.claude/settings.json" "$CLAUDE_TEMPLATE" "claude" || status=1 ;;
            codex)  check_hooks "$HOME/.codex/hooks.json" "$CODEX_TEMPLATE" "codex" || status=1 ;;
            kimi)   check_kimi "$HOME/.kimi-code/config.toml" "$KIMI_TEMPLATE" || status=1 ;;
        esac
        return
    fi
    case "$1" in
        claude) install_hooks "$HOME/.claude/settings.json" "$CLAUDE_TEMPLATE" "claude" ;;
        codex)
            install_hooks "$HOME/.codex/hooks.json" "$CODEX_TEMPLATE" "codex"
            echo "codex: run /hooks inside codex and trust the tmux-agent-state hooks before they run."
            ;;
        kimi) install_kimi "$HOME/.kimi-code/config.toml" "$KIMI_TEMPLATE" ;;
    esac
}

case "${1:-}" in
    claude|codex|kimi)
        step "$1"
        ;;
    "")
        step claude
        step codex
        step kimi
        ;;
    *)
        echo "usage: install.sh [--check] [claude|codex|kimi]" >&2
        exit 1
        ;;
esac
exit "$status"
