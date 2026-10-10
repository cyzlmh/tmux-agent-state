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
# The process-tree walk finds nothing here (the hook is not a descendant of
# any test-server pane), so the foreground-name scan is exercised. The scan
# is a generic substring match on pane_current_command, and only a unique
# match is used — faked with a real foreground process (sleep).
P2="$(tmux_cmd split-window -d -P -F '#{pane_id}' -t ai:main)"
hold_pane "$P2" || fail "hold P2"
TMUX_STATUS_TMUX="tmux -L $SOCK" TMUX_PANE=%999999 bash "$AGENT_STATE" --agent sleep --state busy --detail working
raw=$(get_state "$P2")
echo "$raw" | grep -q '"tool":"sleep"' || fail "fallback should find the agent pane: $raw"
pass "target-pane fallback scans by agent"

# 4b. process-tree fallback: with no usable TMUX_PANE, a hook running inside
#     a pane finds that pane by walking its ancestry to pane_pid (the case
#     where the agent spawns hooks itself). TMUX/TMUX_PANE are stripped so
#     the walk — not the env — is what finds the pane.
P3="$(tmux_cmd split-window -d -P -F '#{pane_id}' -t ai:main)"
tmux_cmd send-keys -t "$P3" \
    "env -u TMUX -u TMUX_PANE TMUX_STATUS_TMUX='tmux -L $SOCK' bash '$AGENT_STATE' --agent codex --state busy --detail working" Enter
raw=""
for _ in $(seq 1 60); do
    raw=$(get_state "$P3")
    [ -n "$raw" ] && break
    sleep 0.05
done
echo "$raw" | grep -q '"state":"busy"' || fail "tree fallback should find the pane: $raw"
pass "target-pane fallback walks the process tree"

# 4c. no unambiguous match -> write nothing. The old fallback wrote to the
#     focused pane (display-message '#{pane_id}'), i.e. state landed on a
#     random pane; assert the current pane and the sleep pane stay untouched.
TMUX_STATUS_TMUX="tmux -L $SOCK" TMUX_PANE=%999999 bash "$AGENT_STATE" --agent definitely-not-running --state busy --detail working
assert_empty "$(get_state "$PANE")" "focused pane must not receive guessed state"
raw=$(get_state "$P2")
echo "$raw" | grep -q '"tool":"sleep"' || fail "unrelated pane must keep its state: $raw"
pass "fallback never writes to a guessed pane"

# 4d. v4 local Codex hooks prove ancestry even with a wrong-but-live env pane.
LOCAL_CODEX_HOME="$(mktemp -d)"
register_tmp_file "$LOCAL_CODEX_HOME"
tmux_cmd send-keys -t "$P3" \
    "printf '%s' '{\"session_id\":\"local-test\"}' | env CODEX_HOME='$LOCAL_CODEX_HOME' TMUX_PANE='$P2' TMUX_STATUS_TMUX='tmux -L $SOCK' bash '$AGENT_STATE' --agent codex --codex-target --state waiting --detail asking" Enter
raw=""
for _ in $(seq 1 60); do
    raw=$(get_state "$P3")
    echo "$raw" | grep -q '"detail":"asking"' && break
    sleep 0.05
done
echo "$raw" | grep -q '"detail":"asking"' || fail "local Codex ancestry routing: $raw"
echo "$(get_state "$P2")" | grep -q '"tool":"sleep"' || fail "local Codex wrote inherited pane"
pass "Codex v4 local hooks ignore wrong live TMUX_PANE"

# 5. templates: valid JSON, all expected events, placeholder replaced by install
for tmpl in claude codex; do
    python3 - "$ROOT_DIR/adapters/$tmpl-hooks.json" "$tmpl" <<'EOF' || fail "template check failed"
import json, sys
path, name = sys.argv[1:3]
d = json.load(open(path))
events = set(d["hooks"].keys())
expected = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
            "PermissionRequest", "Stop", "SessionEnd"}
if name == "claude":
    expected.update({"Elicitation", "ElicitationResult", "Notification",
                     "PostToolUseFailure", "StopFailure"})
if name == "codex":
    # Stop does not fire when a turn is interrupted (Esc), so Interrupt is the
    # only signal that brings the pane back from busy. codex's hook enum
    # (checked against 0.160) has no StopFailure/PostToolUseFailure, and
    # unknown events in hooks.json are silently ignored — subscribing them
    # would be dead config.
    expected.update({"Interrupt"})
assert events == expected, f"{name}: events {events ^ expected}"
version = {"claude": "2", "codex": "4"}[name]
for ev, groups in d["hooks"].items():
    for g in groups:
        cmd = g["hooks"][0]["command"]
        assert "__AGENT_STATE__" in cmd, f"{name}/{ev}: placeholder missing: {cmd}"
        assert "--agent {name}".format(name=name) in cmd or "--clear" in cmd, f"{name}/{ev}: wrong agent: {cmd}"
        assert f"--adapter-version {version}" in cmd, f"{name}/{ev}: version marker missing: {cmd}"
        if name == "codex":
            assert "--codex-target" in cmd, f"codex/{ev}: explicit session routing missing: {cmd}"
        if name == "claude" and "--clear" not in cmd:
            assert "--guard" in cmd, f"claude/{ev}: subagent guard missing: {cmd}"
        # Notification carries many unrelated types; only the needs-input ones
        # may be reported, so the filter must be on.
        if ev == "Notification":
            assert "--notify" in cmd, f"claude/{ev}: notification filter missing: {cmd}"
EOF
    pass "template $tmpl"
done

# 5b. kimi template (TOML): all expected events, placeholder present,
#     commands target the shared script with --agent kimi
python3 - "$ROOT_DIR/adapters/kimi-hooks.toml" <<'EOF' || fail "kimi template check failed"
import re, sys
text = open(sys.argv[1]).read()
expected = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
            "PostToolUseFailure", "PermissionRequest", "PermissionResult",
            "Stop", "StopFailure", "Interrupt", "SessionEnd"}
events = set(re.findall(r'event\s*=\s*"([^"]+)"', text))
assert events == expected, f"kimi: events {events ^ expected}"
# kimi validates config.toml hooks against an enum that has no TurnStarted in
# the installed CLI; one unknown event rejects the whole hooks key.
assert "TurnStarted" not in events, "kimi: TurnStarted is not in the installed enum"
cmds = re.findall(r'command\s*=\s*"([^"]+)"', text)
assert cmds, "kimi: no commands found"
for cmd in cmds:
    assert cmd.startswith("__AGENT_STATE__ "), f"kimi: placeholder missing: {cmd}"
    assert "--agent kimi" in cmd or "--clear" in cmd, f"kimi: wrong agent: {cmd}"
    assert "--adapter-version 2" in cmd, f"kimi: version marker missing: {cmd}"
try:
    import tomllib
except ImportError:
    pass  # python < 3.11: structural regex check above is enough
else:
    d = tomllib.loads(text)
    assert len(d["hooks"]) == len(expected), "kimi: TOML parse mismatch"
EOF
pass "template kimi"

# 5c. adapter payload flags: --guard drops subagent events, --notify only lets
#     needs-input notification types through. Both are driven by the hook JSON
#     on stdin, so exercise them against a stub tmux that records its calls.
STUB_DIR="$(mktemp -d)"
register_tmp_file "$STUB_DIR"
cat > "$STUB_DIR/tmux" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$STUB_CALLS"
case "$1" in
  list-panes) printf '%%1\n' ;;
  display-message) printf '%%1\n' ;;
  set-option|wait-for) exit 0 ;;
esac
exit 0
STUB
chmod +x "$STUB_DIR/tmux"

guard_writes() {  # $1 = hook payload -> number of set-option writes
    local calls; calls="$(mktemp)"
    printf '%s' "$1" | STUB_CALLS="$calls" TMUX_STATUS_TMUX="$STUB_DIR/tmux" \
        TMUX_PANE=%1 TMUX_AGENT_STATE_LOG= bash "$AGENT_STATE" --agent claude --state waiting \
        --detail asking --guard --notify --adapter-version 2 2>/dev/null || true
    local n; n="$(grep -c 'set-option -p' "$calls" 2>/dev/null || true)"
    rm -f "$calls"
    printf '%s' "${n:-0}"
}

[ "$(guard_writes '{"agent_id":"sub1","hook_event_name":"PreToolUse"}')" = 0 ] \
    || fail "subagent payload should be dropped by --guard"
[ "$(guard_writes '{"hook_event_name":"PreToolUse"}')" = 1 ] \
    || fail "main-thread payload should pass --guard"
for t in permission_prompt idle_prompt agent_needs_input elicitation_dialog; do
    [ "$(guard_writes "{\"hook_event_name\":\"Notification\",\"notification_type\":\"$t\"}")" = 1 ] \
        || fail "needs-input notification '$t' should be reported"
done
for t in auth_success agent_completed quota_auto_resume_fired computer_use_enter; do
    [ "$(guard_writes "{\"hook_event_name\":\"Notification\",\"notification_type\":\"$t\"}")" = 0 ] \
        || fail "informational notification '$t' should be dropped by --notify"
done
pass "payload flags: --guard + --notify"

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

# 6d. --check reports adapter versions; legacy wiring (no version marker)
#     is reported as unversioned -> template v2
out=$(HOME="$FAKE_HOME" bash "$ROOT_DIR/adapters/install.sh" --check claude)
echo "$out" | grep -q 'up to date (adapter v2)' || fail "--check up to date: $out"
python3 - "$FAKE_HOME/.claude/settings.json" <<'EOF'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
for groups in d["hooks"].values():
    for g in groups:
        for h in g.get("hooks", []):
            if "agent-state.sh" in h.get("command", ""):
                h["command"] = h["command"].replace(" --adapter-version 2", "")
json.dump(d, open(p, "w"), indent=2)
EOF
if HOME="$FAKE_HOME" bash "$ROOT_DIR/adapters/install.sh" --check claude > "$FAKE_HOME/check.out" 2>&1; then
    fail "--check should exit 1 on drifted (unversioned) wiring"
fi
grep -q 'OUTDATED (installed unversioned -> template v2' "$FAKE_HOME/check.out" \
    || fail "--check drift message: $(cat "$FAKE_HOME/check.out")"
pass "--check reports adapter versions"

# 6e. install drops our entries for events the template no longer carries
#     (e.g. PostToolUseFailure removed in codex template v3) while keeping
#     other tools' hooks on the same event
mkdir -p "$FAKE_HOME/.codex"
cat > "$FAKE_HOME/.codex/hooks.json" <<'EOF'
{"hooks":{
  "PostToolUseFailure":[
    {"hooks":[{"type":"command","command":"/old/agent-state.sh --agent codex --state busy --adapter-version 2"}]},
    {"hooks":[{"type":"command","command":"echo keep-me"}]}
  ],
  "Stop":[{"hooks":[{"type":"command","command":"/old/agent-state.sh --agent codex --clear --adapter-version 2"}]}]
}}
EOF
HOME="$FAKE_HOME" bash "$ROOT_DIR/adapters/install.sh" codex >/dev/null
python3 - "$FAKE_HOME/.codex/hooks.json" <<'EOF' || fail "stale-event cleanup failed"
import json, sys
d = json.load(open(sys.argv[1]))
ptf = d["hooks"].get("PostToolUseFailure", [])
assert all("agent-state.sh" not in json.dumps(g) for g in ptf), f"our stale entry kept: {ptf}"
assert any("keep-me" in json.dumps(g) for g in ptf), "other tool's hook dropped"
stops = json.dumps(d["hooks"]["Stop"])
assert "--adapter-version 4" in stops and "/old/" not in stops, f"Stop not refreshed to v4: {stops}"
EOF
pass "install drops our entries for removed events"

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

# 8. --guard: subagent hook payloads (agent_id present, e.g. claude Task
#    subagents) must not overwrite the main pane's state
run_agent_state "$PANE" --agent claude --state busy --detail working
echo '{"hook_event_name":"PostToolUse","agent_id":"a1b2c3"}' \
    | TMUX_STATUS_TMUX="tmux -L $SOCK" TMUX_PANE="$PANE" \
      bash "$AGENT_STATE" --agent claude --state waiting --detail asking --guard
raw=$(get_state "$PANE")
echo "$raw" | grep -q '"state":"busy"' || fail "subagent payload should be dropped: $raw"
pass "guard drops subagent payload"

# 8b. --guard: main-agent payload (no agent_id) passes through
echo '{"hook_event_name":"PermissionRequest","session_id":"s1"}' \
    | TMUX_STATUS_TMUX="tmux -L $SOCK" TMUX_PANE="$PANE" \
      bash "$AGENT_STATE" --agent claude --state waiting --detail asking --guard
raw=$(get_state "$PANE")
echo "$raw" | grep -q '"detail":"asking"' || fail "main payload should pass the guard: $raw"
pass "guard passes main-agent payload"

# 8c. --guard fails open: unreadable/garbage stdin must not block a write
echo 'not json at all' \
    | TMUX_STATUS_TMUX="tmux -L $SOCK" TMUX_PANE="$PANE" \
      bash "$AGENT_STATE" --agent claude --state busy --detail working --guard
raw=$(get_state "$PANE")
echo "$raw" | grep -q '"state":"busy"' || fail "garbage stdin should fail open: $raw"
pass "guard fails open on garbage"

# 8d. --adapter-version marker is accepted (and ignored) for --check reporting
run_agent_state "$PANE" --agent claude --state busy --detail working --adapter-version 2
raw=$(get_state "$PANE")
echo "$raw" | grep -q '"state":"busy"' || fail "--adapter-version should be ignored: $raw"
pass "--adapter-version accepted"

# 8e. --notify: only needs-input notification types are reported; the rest
#     (auth_success, agent_completed, quota_*, …) must be dropped
run_agent_state "$PANE" --agent claude --state busy --detail working
echo '{"hook_event_name":"Notification","notification_type":"auth_success"}' \
    | TMUX_STATUS_TMUX="tmux -L $SOCK" TMUX_PANE="$PANE" \
      bash "$AGENT_STATE" --agent claude --state waiting --detail asking --notify
raw=$(get_state "$PANE")
echo "$raw" | grep -q '"state":"busy"' || fail "auth_success should be dropped: $raw"
echo '{"hook_event_name":"Notification","notification_type":"permission_prompt"}' \
    | TMUX_STATUS_TMUX="tmux -L $SOCK" TMUX_PANE="$PANE" \
      bash "$AGENT_STATE" --agent claude --state waiting --detail asking --notify
raw=$(get_state "$PANE")
echo "$raw" | grep -q '"detail":"asking"' || fail "permission_prompt should pass: $raw"
pass "--notify filters notification types"

# 9. transition log: every write/clear appends one JSONL line
LOG="$(mktemp)"
register_tmp_file "$LOG"
TMUX_AGENT_STATE_LOG="$LOG" run_agent_state "$PANE" --agent claude --state busy --detail working
TMUX_AGENT_STATE_LOG="$LOG" run_agent_state "$PANE" --clear
python3 - "$LOG" "$PANE" <<'EOF' || fail "transition log check failed"
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
assert len(rows) == 2, f"expected 2 log lines, got {len(rows)}: {rows}"
assert rows[0]["pane"] == sys.argv[2] and rows[0]["state"] == "busy", rows[0]
assert rows[0]["tool"] == "claude" and rows[0]["detail"] == "working", rows[0]
assert isinstance(rows[0]["ts"], (int, float)), rows[0]
assert rows[1]["state"] == "cleared", rows[1]
EOF
pass "transition log (write + clear)"

echo "PASS: test-agent"
