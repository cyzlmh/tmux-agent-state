# pi adapter — `agent-state.ts`

A pi extension that reports this pi's state (waiting / busy) to tmux-agent-state via
the [`@agent-state`](../../PROTOCOL.md) pane option.

## Event → state mapping

| pi event                         | state    | detail   |
| -------------------------------- | -------- | -------- |
| `session_start`                  | waiting  | ready    |
| `input` / `before_agent_start` / `agent_start` | busy | working |
| `agent_settled` (turn fully done) | waiting  | done     |
| `session_shutdown`               | (clears the option) | - |

Only writes on state transitions (no heartbeat — liveness is decided by the
reader via the pane foreground command, see PROTOCOL.md), so it never spams
tmux on per-token events.

Reliability: `state` is driven by deterministic events only — see the
`question` integration below for how "agent is asking the user" is
reported reliably (no tool-name guessing). `detail` is a display hint
except `asking`, which is written by the blocking tool itself.

## Optional: question tool (reliable "asking")

`agent-state.ts` alone is fully functional — a pi blocked on a question just
shows `busy`. For the agent to show up as *asking* (needs-input) instead,
a blocking tool must report it: the tool itself knows it is waiting, so this
is reliable (no tool-name guessing).

`question.ts` in this directory is a full-custom-UI example of the contract
(options list + inline editor, via `ctx.ui.custom()`): it writes `waiting` +
`detail=asking` before blocking on the user, then restores `busy` + `working`
in a `finally` block. Each state write also refreshes the window-label chips
via colorize.sh (same mechanism as agent-state.ts), so a chip moves
asking→running→done instead of skipping the brief running state. Load it only
if you want asking reporting (it requires `agent-state.ts`, which owns the
initial state and shutdown cleanup):

```sh
ln -s ~/tmux-agent-state/adapters/pi/question.ts \
      ~/.pi/agent/extensions/question.ts
```

A shared in-process flag (`globalThis.__tmuxPanelQuestion`) coordinates the
two extensions: while the question is open, `writeState` reports
`waiting/asking` (and its `since`) instead of the in-memory `busy`. On
question close, the tool clears the flag and restores `busy/working`;
`agent_settled` then reports `waiting/done` as usual.

Have your own blocking tool already? Don't install the example — wrap the
blocking call with the same contract (set the flag + write waiting/asking
before, clear + restore busy/working in `finally`), see the header of
`question.ts`.

## Load

Dev (one-off):

```sh
pi --extension ~/tmux-agent-state/adapters/pi/agent-state.ts   # path of your clone
```

Installed (picked up by every pi in this project):

```sh
ln -s ~/tmux-agent-state/adapters/pi/agent-state.ts \
      ~/.pi/agent/extensions/agent-state.ts
# then in a running pi:  /reload   (or restart pi)
```

No-op when not inside tmux (`$TMUX_PANE` unset).

## Type-check

The extensions import types from the globally-installed
`@earendil-works/pi-coding-agent`, plus `typebox` and `@earendil-works/pi-tui`
(transitive dependencies). A few symlinks make `tsc` resolve them without a
full npm install:

```sh
cd tmux-agent-state
mkdir -p node_modules/@earendil-works node_modules/@types
ln -sfh "$(npm root -g)/@earendil-works/pi-coding-agent" node_modules/@earendil-works/pi-coding-agent
ln -sfh "$(npm root -g)/@earendil-works/pi-coding-agent/node_modules/typebox" node_modules/typebox
ln -sfh "$(npm root -g)/@earendil-works/pi-coding-agent/node_modules/@earendil-works/pi-tui" node_modules/@earendil-works/pi-tui
ln -sfh "$(npm root -g)/@types/node" node_modules/@types/node
bunx tsc -p tsconfig.json
```

## Verify end-to-end

Run a pi with the extension in a scratch tmux pane and watch the option change:

```sh
EXT="$PWD/adapters/pi/agent-state.ts"   # run from the repo root
P=$(tmux new-window -c /tmp -P -F '#{pane_id}' -n tmuxpanel-test)
tmux send-keys -t "$P" "pi --extension $EXT" C-m
sleep 3; tmux display -p -t "$P" "#{@agent-state}"   # -> state=waiting
tmux send-keys -t "$P" "hi" Enter
sleep 1; tmux display -p -t "$P" "#{@agent-state}"   # -> state=busy, then waiting
tmux send-keys -t "$P" "/exit" C-m; sleep 0.5; tmux kill-window -t tmuxpanel-test
```

Verified transitions: `session_start`→waiting, `input`/`agent_start`→busy,
`agent_settled`→waiting, `session_shutdown` clears the option.
