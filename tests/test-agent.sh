#!/usr/bin/env bash
# Integration tests: the hook-based adapter (adapters/agent-state.sh), the
# claude/codex/kimi hook templates, and install.sh merge behaviour.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/tests/lib/tmux-test-lib.sh"

trap cleanup_test_server EXIT
setup_test_server "agent"

NOW=$(date +%s)
AGENT_STATE="$ROOT_DIR/adapters/agent-state.sh"

run_agent_state() {  # $1 pane, rest = args
    local pane="$1"
    shift
    TMUX_STATUS_TMUX="tmux -L $SOCK" TMUX_PANE="$pane" bash "$AGENT_STATE" "$@"
}

get_state() {
    tmux_cmd show-options -pqv -t "$1" @agent-state 2>/dev/null || true
}

# 1. write busy/working
run_agent_state "$PANE" --agent claude --state busy --detail working
raw=$(get_state "$PANE")
echo "$raw" | grep -q '"tool":"claude"' || fail "tool field: $raw"
echo "$raw" | grep -q '"state":"busy"' || fail "state field: $raw"
echo "$raw" | grep -q '"detail":"working"' || fail "detail field: $raw"
echo "$raw" | grep -q '"ts":' || fail "ts field: $raw"
pass "write busy/working"

# 2. transition to waiting/asking (permission request)
run_agent_state "$PANE" --agent claude --state waiting --detail asking
raw=$(get_state "$PANE")
echo "$raw" | grep -q '"state":"waiting"' || fail "waiting state: $raw"
echo "$raw" | grep -q '"detail":"asking"' || fail "asking detail: $raw"
pass "write waiting/asking"

# 3. clear
run_agent_state "$PANE" --clear
[ -z "$(get_state "$PANE")" ] || fail "clear should unset: $(get_state "$PANE")"
pass "clear"

# 4. target-pane fallback: invalid $TMUX_PANE, agent running in another pane.
# pane_current_command is the real process name (not argv[0]), so we fake the
# agent match with a real foreground process; the scan itself is generic
# (substring match on the agent name).
P2="$(tmux_cmd split-window -d -P -F '#{pane_id}' -t ai:main)"
hold_pane "$P2" || fail "hold P2"
TMUX_STATUS_TMUX="tmux -L $SOCK" TMUX_PANE=%999999 bash "$AGENT_STATE" --agent sleep --state busy --detail working
raw=$(get_state "$P2")
echo "$raw" | grep -q '"tool":"sleep"' || fail "fallback should find the agent pane: $raw"
pass "target-pane fallback scans by agent"

# 5. templates: valid JSON, all expected events, placeholder replaced by install
for tmpl in claude codex; do
    python3 - "$ROOT_DIR/adapters/$tmpl-hooks.json" "$tmpl" <<'EOF' || fail "template check failed"
import json, sys
path, name = sys.argv[1:3]
d = json.load(open(path))
events = set(d["hooks"].keys())
expected = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest", "Stop", "SessionEnd"}
if name == "claude":
    expected.update({"Elicitation", "ElicitationResult"})
missing = expected - events
assert not missing, f"{name}: missing events {missing}"
for ev, groups in d["hooks"].items():
    for g in groups:
        cmd = g["hooks"][0]["command"]
        assert "__AGENT_STATE__" in cmd, f"{name}/{ev}: placeholder missing: {cmd}"
        assert "--agent {name}".format(name=name) in cmd or "--clear" in cmd, f"{name}/{ev}: wrong agent: {cmd}"
EOF
    pass "template $tmpl"
done

# 5b. kimi template (TOML): all expected events, placeholder present,
#     commands target the shared script with --agent kimi
python3 - "$ROOT_DIR/adapters/kimi-hooks.toml" <<'EOF' || fail "kimi template check failed"
import re, sys
text = open(sys.argv[1]).read()
expected = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
            "PermissionRequest", "PermissionResult", "Stop", "StopFailure", "Interrupt", "SessionEnd"}
events = set(re.findall(r'event\s*=\s*"([^"]+)"', text))
missing = expected - events
assert not missing, f"kimi: missing events {missing}"
cmds = re.findall(r'command\s*=\s*"([^"]+)"', text)
assert cmds, "kimi: no commands found"
for cmd in cmds:
    assert cmd.startswith("__AGENT_STATE__ "), f"kimi: placeholder missing: {cmd}"
    assert "--agent kimi" in cmd or "--clear" in cmd, f"kimi: wrong agent: {cmd}"
try:
    import tomllib
except ImportError:
    pass  # python < 3.11: structural regex check above is enough
else:
    d = tomllib.loads(text)
    assert len(d["hooks"]) == len(expected), "kimi: TOML parse mismatch"
EOF
pass "template kimi"

# 6. install.sh merges into a fake HOME, idempotent, preserves unrelated hooks
FAKE_HOME="$(mktemp -d)"
register_tmp_file "$FAKE_HOME"
mkdir -p "$FAKE_HOME/.claude"
printf '{"hooks":{"OtherEvent":[{"hooks":[{"type":"command","command":"echo keep"}]}]}}' \
    > "$FAKE_HOME/.claude/settings.json"
HOME="$FAKE_HOME" bash "$ROOT_DIR/adapters/install.sh" claude >/dev/null
python3 - "$FAKE_HOME/.claude/settings.json" <<'EOF' || fail "install merge failed"
import json, sys
d = json.load(open(sys.argv[1]))
assert "OtherEvent" in d["hooks"], "unrelated hook lost"
assert "Stop" in d["hooks"] and "SessionStart" in d["hooks"], "missing our events"
stops = json.dumps(d["hooks"]["Stop"])
assert "agent-state.sh" in stops and "__AGENT_STATE__" not in stops, f"placeholder not replaced: {stops}"
assert "--agent claude" in stops, "wrong agent in Stop hook"
EOF
HOME="$FAKE_HOME" bash "$ROOT_DIR/adapters/install.sh" claude >/dev/null   # idempotent
python3 - "$FAKE_HOME/.claude/settings.json" <<'EOF' || fail "install not idempotent"
import json, sys
d = json.load(open(sys.argv[1]))
for ev, groups in d["hooks"].items():
    assert sum(1 for g in groups if "agent-state.sh" in json.dumps(g)) <= 1, \
        f"{ev}: duplicate tmux-agent-state entries after reinstall"
EOF
pass "install merge + idempotent"

# 6b. install.sh kimi: appends a marked block to config.toml, preserves the
#     user's TOML (existing hooks included), idempotent on re-run
mkdir -p "$FAKE_HOME/.kimi-code"
printf 'model = "k2"\n\n[[hooks]]\nevent = "Notification"\ncommand = "echo keep"\n' \
    > "$FAKE_HOME/.kimi-code/config.toml"
HOME="$FAKE_HOME" bash "$ROOT_DIR/adapters/install.sh" kimi >/dev/null
python3 - "$FAKE_HOME/.kimi-code/config.toml" <<'EOF' || fail "kimi install merge failed"
import sys
text = open(sys.argv[1]).read()
assert 'model = "k2"' in text, "user TOML lost"
assert 'command = "echo keep"' in text, "existing hook lost"
assert "agent-state.sh" in text and "__AGENT_STATE__" not in text, "placeholder not replaced"
assert "--agent kimi" in text, "wrong agent"
assert text.count("# >>> tmux-agent-state >>>") == 1, "block marker missing/duplicated"
try:
    import tomllib
except ImportError:
    pass
else:
    d = tomllib.loads(text)
    assert d["model"] == "k2", "user TOML corrupted"
    assert any(h["command"] == "echo keep" for h in d["hooks"]), "existing hook lost in parse"
EOF
HOME="$FAKE_HOME" bash "$ROOT_DIR/adapters/install.sh" kimi >/dev/null   # idempotent
python3 - "$FAKE_HOME/.kimi-code/config.toml" <<'EOF' || fail "kimi install not idempotent"
import sys
text = open(sys.argv[1]).read()
assert text.count("# >>> tmux-agent-state >>>") == 1, "block duplicated after reinstall"
assert text.count('event = "SessionStart"') == 1, "hooks duplicated after reinstall"
assert 'command = "echo keep"' in text, "existing hook lost after reinstall"
EOF
pass "kimi install merge + idempotent"

# 6c. kimi install into a fresh HOME creates the config from scratch
rm -rf "$FAKE_HOME/.kimi-code"
HOME="$FAKE_HOME" bash "$ROOT_DIR/adapters/install.sh" kimi >/dev/null
grep -q '# >>> tmux-agent-state >>>' "$FAKE_HOME/.kimi-code/config.toml" \
    || fail "kimi install should create config.toml"
pass "kimi install creates config"

# 7. install.sh refuses to overwrite an unparseable existing config (would
#    otherwise silently wipe the user's settings)
printf '{invalid json' > "$FAKE_HOME/.claude/settings.json"
if HOME="$FAKE_HOME" bash "$ROOT_DIR/adapters/install.sh" claude >/dev/null 2>&1; then
    fail "install should refuse invalid JSON"
fi
[ "$(cat "$FAKE_HOME/.claude/settings.json")" = '{invalid json' ] \
    || fail "config was modified despite refusal"
pass "install refuses invalid JSON"

echo "PASS: test-agent"
