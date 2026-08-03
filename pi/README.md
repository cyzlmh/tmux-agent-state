# pi adapter — `agent-state.ts`

A pi extension that reports this pi's state (waiting / busy) to tmux-agent-state via
the [`@agent-state`](../PROTOCOL.md) pane option.

## Event → state mapping

| pi event                         | state    | detail   |
| -------------------------------- | -------- | -------- |
| `session_start`                  | waiting  | ready    |
| `input` / `before_agent_start` / `agent_start` | busy | working |
| `agent_settled` (turn fully done) | waiting  | done     |
| `session_shutdown`               | (clears the option) | - |

Only writes on state transitions + a 15s heartbeat, so it never spams tmux on
per-token events.

Reliability: `state` is driven by deterministic events only — see the
`question` integration below for how "agent is asking the user" is
reported reliably (no tool-name guessing). `detail` is a display hint
except `asking`, which is written by the blocking tool itself.

## question tool integration (reliable "asking")

The `question` extension (`~/.pi/agent/extensions/question.ts`) reports
`waiting` + `detail=asking` while its `ctx.ui.custom()` blocks on the user,
then restores `busy` + `working` in a `finally` block. This is reliable
because the tool itself knows it is waiting — no name matching.

A shared in-process flag (`globalThis.__tmuxPanelQuestion`) coordinates the
two extensions: while the question is open, this extension's 15s heartbeat
rewrites `waiting/asking` (keeping `ts` fresh) instead of overwriting it
with the in-memory `busy`. On `question` close, the tool clears the flag and
restores `busy/working`; `agent_settled` then reports `waiting/done` as
usual.

## Load

Dev (one-off):

```sh
pi --extension ~/tmux-agent-state/pi/agent-state.ts   # path of your clone
```

Installed (picked up by every pi in this project):

```sh
ln -s ~/tmux-agent-state/pi/agent-state.ts \
      ~/.pi/agent/extensions/agent-state.ts
# then in a running pi:  /reload   (or restart pi)
```

No-op when not inside tmux (`$TMUX_PANE` unset).

## Type-check

The extension imports types from the globally-installed `@earendil-works/pi-coding-agent`.
A couple of symlinks make `tsc` resolve them without a full npm install:

```sh
cd tmux-agent-state
mkdir -p node_modules/@earendil-works node_modules/@types
ln -sfh "$(npm root -g)/@earendil-works/pi-coding-agent" node_modules/@earendil-works/pi-coding-agent
ln -sfh "$(npm root -g)/@types/node" node_modules/@types/node
bunx tsc -p tsconfig.json
```

## Verify end-to-end

Run a pi with the extension in a scratch tmux pane and watch the option change:

```sh
EXT="$PWD/pi/agent-state.ts"   # run from the repo root
P=$(tmux new-window -c /tmp -P -F '#{pane_id}' -n tmuxpanel-test)
tmux send-keys -t "$P" "pi --extension $EXT" C-m
sleep 3; tmux display -p -t "$P" "#{@agent-state}"   # -> state=waiting
tmux send-keys -t "$P" "hi" Enter
sleep 1; tmux display -p -t "$P" "#{@agent-state}"   # -> state=busy, then waiting
tmux send-keys -t "$P" "/exit" C-m; sleep 0.5; tmux kill-window -t tmuxpanel-test
```

Verified transitions: `session_start`→waiting, `input`/`agent_start`→busy,
`agent_settled`→waiting, 15s heartbeat refreshes `ts`, `session_shutdown`
clears the option.
