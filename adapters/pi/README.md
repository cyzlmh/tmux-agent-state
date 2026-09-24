# pi adapter — `agent-state.ts`

A pi extension that reports this pi's state (waiting / busy) to tmux-agent-state via
the [`@agent-state`](../../PROTOCOL.md) pane option.

## Event → state mapping

| pi event                         | state    | detail   |
| -------------------------------- | -------- | -------- |
| `session_start`                  | waiting  | ready    |
| `input` / `before_agent_start` / `agent_start` | busy | working |
| `ui_prompt_start` / `ui_prompt_end` | waiting / busy | asking / working |
| `agent_settled` (turn fully done) | waiting  | done     |
| `agent_settled` + `stopReason: "length"` | waiting | truncated |
| `agent_settled` + `stopReason: "error"`  | waiting | error |
| `session_shutdown`               | (clears the option) | - |

The last assistant message's `stopReason` decides the settled detail: a turn
cut off at the model's output limit (`length`, which pi renders in-transcript
as "Response was truncated before completion.") reports `truncated`, and a
failed run (`error`) reports `error`, so neither looks like a clean `done`. A
user abort (`aborted`) is deliberate and stays `done`.

Only writes on state transitions (no heartbeat — liveness is decided by the
reader via the pane foreground command, see PROTOCOL.md), so it never spams
tmux on per-token events. `ui_prompt_start`/`ui_prompt_end` are the one
exception: they are notifications rather than transitions, so they rewrite the
option directly (bracketed by a depth counter, so nested prompts are fine).

Reliability: `state` is driven by deterministic events only, and "the agent is
asking" comes from pi's own signal — `ui_prompt_start`/`ui_prompt_end` fire
around every blocking `ctx.ui` prompt (pi ≥ 0.84.4), whatever extension raised
it. There is no tool-name guessing. `detail` is a display hint except
`asking`, which is only written when the agent really is blocked on the user.

## "Asking" reporting

As of pi 0.84.4, `agent-state.ts` alone reports needs-input: pi emits
`ui_prompt_start`/`ui_prompt_end` around every blocking `ctx.ui` call
(`select`/`confirm`/`input`/`editor`/`custom`), which covers your own blocking
tools as well as the built-in prompts of other extensions. Nothing else is
needed for the common case.

Two caveats:

- pi dispatches these events on a microtask, and it only wraps calls made
  through `ctx.ui`. A dialog drawn by pi's own core (outside the extension
  runner) is not covered.
- On pi < 0.84.4 the events do not exist, so `agent-state.ts` still honours the
  shared `globalThis.__tmuxPanelQuestion` flag described below.

## Optional: question tool (the pi < 0.84.4 path, and a UI example)

`question.ts` in this directory is a full-custom-UI example (options list +
inline editor, via `ctx.ui.custom()`). It sets the shared
`globalThis.__tmuxPanelQuestion` flag and writes `waiting` + `detail=asking`
before blocking on the user, then restores `busy` + `working` in a `finally`
block. Each state write also refreshes the window-label chips via colorize.sh
(same mechanism as agent-state.ts), so a chip moves asking→running→done
instead of skipping the brief running state. Load it if you want that UI, or if
you are on a pi older than 0.84.4 (it requires `agent-state.ts`, which owns the
initial state and shutdown cleanup):

```sh
ln -s ~/tmux-agent-state/adapters/pi/question.ts \
      ~/.pi/agent/extensions/question.ts
```

A shared in-process flag (`globalThis.__tmuxPanelQuestion`) coordinates the
two extensions: while the question is open, `writeState` reports
`waiting/asking` (and its `since`) instead of the in-memory `busy`. On
question close, the tool clears the flag and restores `busy/working`;
`agent_settled` then reports `waiting/done` as usual. On pi ≥ 0.84.4 the
`ui_prompt_*` events already cover this, so the flag is redundant there (it is
harmless — `writeState` treats either signal as asking).

Have your own blocking tool already? On pi ≥ 0.84.4 you get asking reporting for
free as soon as you use `ctx.ui.*`. If you are on an older pi, or want to report
asking without a dialog, set the flag and write waiting/asking before blocking,
then clear + restore busy/working in `finally` — see the header of
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
