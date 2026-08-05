# tmux-agent-state

Let CLI agents running in tmux panes (pi / claude code / codex / kimi code)
report whether
they are **waiting for user input** or **busy**, so an external visualiser /
monitor does not have to guess from screen content.

## Layout

```
tmux-agent-state/
  PROTOCOL.md          the shared @agent-state contract (read this first)
  tsconfig.json        type-check setup for the adapters
  adapters/
    agent-state.sh     shared hook adapter: writes @agent-state from claude/codex events
    claude-hooks.json  claude hook template (SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/
                       PermissionRequest/Elicitation/ElicitationResult/Stop/SessionEnd)
    codex-hooks.json   codex hook template (same minus Elicitation/ElicitationResult)
    kimi-hooks.toml    kimi hook template (SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/
                       PermissionRequest/PermissionResult/Stop/StopFailure/Interrupt/SessionEnd)
    install.sh         merges the hooks into ~/.claude/settings.json / ~/.codex/hooks.json /
                       ~/.kimi-code/config.toml
    pi/
      agent-state.ts   pi extension (implemented)
      question.ts      optional example: asking reporting for blocking tools
      README.md        how to load + test it
  statusbar/
    statusbar.tmux        tmux bootstrap (interpolates #{agent_status} + window-status formats)
    scripts/indicator.py  status segment: session-level agent statistics (?/✓/!/▶)
    scripts/colorize.sh   window-label colour chips: one per agent pane, in layout order
  tests/               tmux-socket integration + unit tests (bash tests/run-all.sh)
```

Consumers: the status-bar segment in `statusbar/` (tmux-native, aligned with
[tmux-agent-indicator](https://github.com/accessd/tmux-agent-indicator)'s
model), and a web visualiser (work in progress, not yet in this repo) — both
trust `@agent-state` when fresh and report `stale` / `untracked` explicitly
for everything else (no heuristics).

## Install

Requirements: tmux ≥ 3.0, python3 (status segment + chips), and
[pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) for the
agent adapter (the status bar runs without pi, but then has nothing to track).

```sh
git clone https://github.com/cyzlmh/tmux-agent-state.git ~/tmux-agent-state

# 1. status bar: add the placeholder, then load the bootstrap once per server
tmux set -g status-right '#{agent_status} | %H:%M'
tmux run-shell ~/tmux-agent-state/statusbar/statusbar.tmux

# 2. pi adapter: symlink the extension, then /reload inside pi
ln -s ~/tmux-agent-state/adapters/pi/agent-state.ts ~/.pi/agent/extensions/agent-state.ts
# optional: also symlink question.ts for asking reporting (see adapters/pi/README.md)

# 3. claude/codex/kimi adapters (optional): merges hooks into their configs
~/tmux-agent-state/adapters/install.sh          # all, or: install.sh claude | codex | kimi
# after installing codex hooks: run /hooks inside codex and trust them
```

## Status bar

The tmux-native consumer, two complementary surfaces driven by the same
`@agent-state` pane option (no env-var state; staleness detection kept —
agent crashes surface as `stale` instead of a fake state that lingers):

```tmux
set -g status-right '#{agent_status} | %H:%M'
tmux run-shell ~/tmux-agent-state/statusbar/statusbar.tmux   # once per server
```

The bootstrap also owns the bar's look: if `status-style` is still tmux's
factory default (`bg=green,fg=black` — which makes green/blue states
unreadable), it is set to `bg=colour234,fg=colour250`, the dark background
the state colours are designed for. A customised `status-style` is left
untouched; `set -g @agent-status-theme off` opts out entirely.

**1. Statistics segment (status-right)** — the status bar shows the same
content in every window (it is scoped to the session), so it reports
aggregate counts instead of per-pane detail:

```
?3 ✓2 !1 ▶1     3 asking (waiting for your input) / 2 done / 1 stale / 1 running
```

Zero counts are omitted; an empty session shows nothing. Config:
`@agent-status-scope` (session|window), `@agent-status-color-*` (defaults:
needs-input colour214, done colour34, stale colour161, running colour39),
`@agent-status-theme` (on|off).

**2. Window-label colour chips** — each window's label in the window list
shows one colour chip per agent pane, in pane layout order, so a glance at
the labels tells you how many agents each window holds and what state each
is in:

```
[0] train ▮▮▮   3 agent panes: done / asking / done
[1] tmux  ▮     1 agent: asking
```

Chips are pure information (no border/title styling, nothing to reset on
focus). The window-status formats are extended once by the bootstrap
(idempotent append — your own format customisation is preserved). The pi
extension spawns `colorize.sh` after state transitions — and the blocking
tools that write state themselves (e.g. `question.ts`) do too — to refresh
chips (`TMUX_STATUS_COLORIZE` overrides the path; empty string disables), and the
status segment re-runs it on status-bar redraws (throttled to 15s) — that
covers staleness and adapter shutdown, which are not transitions and would
otherwise leave a dead agent's chip stuck at its last colour.

Wire state -> display mapping (deterministic facts only):

| @agent-state            | display            |
| ----------------------- | ------------------ |
| `busy`                  | running (blue)     |
| `waiting` + asking      | needs-input (yellow, bold)
| `waiting` + done        | done (green)       |
| `waiting` + ready       | (not counted)      |
| adapter present, pane foreground is a shell | stale (red) |

`state` comes from adapter events only (input/agent_start -> busy,
agent_settled -> waiting); `detail=asking` is reliable too — the pi
`question` tool (optional example, `adapters/pi/question.ts`) writes
waiting/asking while it blocks on the user (see
`adapters/pi/README.md`). Other `detail` values are display hints only.

## Status

| adapter | form | status |
| ------- | ---- | ------ |
| pi      | TS extension (`adapters/pi/agent-state.ts`) | done, e2e verified |
| claude  | hooks (`adapters/claude-hooks.json`) | done — `adapters/install.sh claude` |
| codex   | hooks (`adapters/codex-hooks.json`) | done — `adapters/install.sh codex`, then trust in `/hooks` |
| kimi    | hooks (`adapters/kimi-hooks.toml`) | done — `adapters/install.sh kimi` |
| zsh     | none (`pane_current_command` ⇒ waiting, exact) | n/a |

## Verify

```sh
cd tmux-agent-state
# type-check (needs the pi package + @types/node resolvable; see adapters/pi/README.md)
bunx tsc -p tsconfig.json
# status segment + bootstrap on an isolated tmux socket
bash tests/run-all.sh
```

## License

MIT — see [LICENSE](LICENSE).
