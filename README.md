# tmux-agent-state

[![test](https://github.com/cyzlmh/tmux-agent-state/actions/workflows/test.yml/badge.svg)](https://github.com/cyzlmh/tmux-agent-state/actions/workflows/test.yml)

Let CLI agents running in tmux panes (pi / claude code / codex / kimi code)
report whether
they are **waiting for user input** or **busy**, so an external visualiser /
monitor does not have to guess from screen content.

## Layout

```
tmux-agent-state/
  PROTOCOL.md          the shared @agent-state contract (read this first)
  tmux-agent-state.tmux  TPM entry point (delegates to statusbar/statusbar.tmux)
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
    scripts/agent_state.py  shared classifier: the PROTOCOL.md reader rules, one module
                            used by the segment, chips, explain, wait and tmux-viz
    scripts/indicator.py  status segment: session-level agent statistics (?/✓/!/▶)
    scripts/colorize.sh   window-label colour chips: one per agent pane, in layout order
    scripts/explain.py    why does a pane show its state? (explain.py <pane_id> [--history N])
    scripts/wait.py       block until a pane reaches a state (wait.py %12 needs-input --timeout 60)
  examples/
    notify-on-input.sh  desktop notification when an agent pane starts waiting
                        for input (event-driven via the global wait-for channel)
  tmux-viz/
    tmux_viz.py           web visualiser: overview cards + per-window detail, same
                          reader rules as the status bar (python3 tmux-viz/tmux_viz.py)
  tests/               tmux-socket integration + unit tests (bash tests/run-all.sh)
```

Consumers: the status-bar segment in `statusbar/` (tmux-native, aligned with
[tmux-agent-indicator](https://github.com/accessd/tmux-agent-indicator)'s
model), and the web visualiser in `tmux-viz/` — both follow the PROTOCOL.md
reader rules (a live adapter's state is trusted regardless of age; liveness is
the pane foreground command, never `ts`) and report `stale` / `untracked`
explicitly for everything else (no heuristics).

## Install

Requirements: tmux ≥ 3.0, python3 (status segment + chips), and
[pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) for the
agent adapter (the status bar runs without pi, but then has nothing to track).

```sh
git clone https://github.com/cyzlmh/tmux-agent-state.git ~/tmux-agent-state

# 1. status bar: add the placeholder, then load the bootstrap once per server
tmux set -g status-right '#{agent_status}'
tmux run-shell ~/tmux-agent-state/statusbar/statusbar.tmux

# 2. pi adapter: symlink the extension, then /reload inside pi
ln -s ~/tmux-agent-state/adapters/pi/agent-state.ts ~/.pi/agent/extensions/agent-state.ts
# optional: also symlink question.ts for asking reporting (see adapters/pi/README.md)

# 3. claude/codex/kimi adapters (optional): merges hooks into their configs
~/tmux-agent-state/adapters/install.sh          # all, or: install.sh claude | codex | kimi
# after installing codex hooks: run /hooks inside codex and trust them
# later, after git pull: install.sh --check reports wiring drift and adapter
# versions (installed vX -> template vY), no writes
```

The `tmux set -g` commands above only apply to the running server; to keep
the status bar across restarts, put both lines in `~/.tmux.conf` and reload
with `tmux source-file ~/.tmux.conf`. The placeholder is plain text — append
your own segments after it if you want them (e.g. `'#{agent_status} | %H:%M'`
for a clock); the bootstrap interpolates in place and leaves the rest of the
value untouched.

### With TPM (optional)

With [TPM](https://github.com/tmux-plugins/tpm), the clone and the
status-bar bootstrap collapse into one plugin line in `~/.tmux.conf`:

```tmux
set -g @plugin 'cyzlmh/tmux-agent-state'
set -g status-right '#{agent_status}'   # the placeholder contract is unchanged
```

`prefix + I` clones the repo and runs the top-level `tmux-agent-state.tmux`,
which delegates to the same bootstrap. TPM tracks the default branch and
re-runs the entry on every `tmux source-file ~/.tmux.conf` — the bootstrap
is idempotent, so reloads are safe. Agent-side wiring (steps 2-3 above) is
deliberately not part of plugin load: run `adapters/install.sh` from
`~/.tmux/plugins/tmux-agent-state` yourself.

## Status bar

The tmux-native consumer, two complementary surfaces driven by the same
`@agent-state` pane option (no env-var state; staleness detection kept —
agent crashes surface as `stale` instead of a fake state that lingers):

```tmux
set -g status-right '#{agent_status}'
tmux run-shell ~/tmux-agent-state/statusbar/statusbar.tmux   # once per server
```

Want a clock or other segments? Append them after the placeholder — the
bootstrap only replaces `#{agent_status}` and leaves the rest of the value
untouched.

The bootstrap also owns the bar's look: if `status-style` is still tmux's
factory default (`bg=green,fg=black` — which makes green/blue states
unreadable), it is set to `bg=colour234,fg=colour250`, the dark background
the state colours are designed for. A customised `status-style` is left
untouched. The factory `window-status-separator` (a single space) becomes a
visible `|` so one window's chips don't blur into the next window's label;
a customised separator is left untouched. `set -g
@agent-status-theme off` opts out entirely.

**1. Statistics segment (status-right)** — the status bar shows the same
content in every window (it is scoped to the session), so it reports
aggregate counts instead of per-pane detail:

```
?3 ✓2 !1 ▶1     3 asking (waiting for your input) / 2 done / 1 stale / 1 running
```

Zero counts are omitted; an empty session shows nothing. Config:
`@agent-status-scope` (session|window), `@agent-status-color-*` (defaults:
needs-input colour180, done colour108, stale colour167, running colour68),
`@agent-status-theme` (on|off).

**2. Window-label colour chips** — each window's label in the window list
shows one colour chip per agent pane, in pane layout order, so a glance at
the labels tells you how many agents each window holds and what state each
is in:

```
0:train▮▮▮|   3 agent panes: done / asking / done
1:tmux▮|       1 agent: asking
```

The chips are glued to the window name (no gap to mistake for a window
boundary) and the `|` marks the boundary to the next window — no extra
width is added to the labels.

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
| `waiting` + asking      | needs-input (sand, bold)
| `waiting` + done        | done (sage)       |
| `waiting` + ready       | (not counted)      |
| adapter present, pane foreground is a shell | stale (soft red) |

`state` comes from adapter events only (input/agent_start -> busy,
agent_settled -> waiting); `detail=asking` is reliable too — the pi
`question` tool (optional example, `adapters/pi/question.ts`) writes
waiting/asking while it blocks on the user (see
`adapters/pi/README.md`). Other `detail` values are display hints only.

## Design language

One palette, shared by every consumer: the status bar uses tmux 256-colour
values, the web visualiser (`tmux-viz/`) uses their hex equivalents, so a
state is the same colour in both. Chrome is monochrome — state colours are
the only colours.

| display state | meaning                       | tmux      | hex       | symbol   |
| ------------- | ----------------------------- | --------- | --------- | -------- |
| needs-input   | agent is asking for input     | colour180 | `#d7af87` | `?` bold |
| done          | agent finished a turn         | colour108 | `#87af87` | `✓`      |
| running       | agent is busy                 | colour68  | `#5f87d7` | `▶`      |
| stale         | adapter gone, state lingering | colour167 | `#d75f5f` | `!`      |
| ready / shell / untracked / dead | informational only | — | muted grey | `·` / `✕` |

Chrome: background `#1c1c1c` (colour234), panel `#262626`, lines `#3a3a3a`,
text `#bcbcbc` (colour250), muted `#808080`.

Principles:

- **needs-input is the only attention colour** — the only bold/accented
  element anywhere. done/running are information, stale is an anomaly,
  everything else stays muted.
- **explicit over implicit** — stale/untracked are shown as-is, never
  papered over with a friendlier colour.
- **minimal chrome** — no glow, no tinted borders, no badge pills; a 1px
  neutral border plus a coloured symbol carries the state.

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

CI runs the same suite on macOS and Ubuntu for every push to main. TPM
tracks the default branch, so main is the release channel and must stay
green — `git config core.hooksPath .githooks` installs a pre-push hook that
gates pushes on the full suite.

## License

MIT — see [LICENSE](LICENSE).
