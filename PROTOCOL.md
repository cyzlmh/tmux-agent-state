# tmux-agent-state protocol

A tiny contract so CLI agents (pi / claude code / codex / …) running inside tmux
panes can report **"am I waiting for the user or not"** to an external
visualiser/monitor, without the visualiser having to guess from screen content.

## Goal

Two states only:

| state    | meaning                                              |
| -------- | --------------------------------------------------- |
| `waiting`| the pane is blocked waiting for user input         |
| `busy`   | something is happening (model call, tool, command)  |

(`dead` is detected by the reader via `pane_dead`; adapters do not emit it.)

An adapter writes its state; the reader trusts it when fresh. When it goes
stale the reader says so explicitly (`stale`) instead of guessing from screen
content. Explicit is better than implicit: no ps / screen-scraping heuristics.

## Transport

A **tmux pane-scoped user option** named `@agent-state`.

tmux sets the env var `$TMUX_PANE` (e.g. `%287`) for every process running in a
pane, so an adapter always knows which pane to write.

Write (adapter, from inside the pane):

```sh
tmux set-option -p -t "$TMUX_PANE" @agent-state '<json>'
```

Clear on shutdown:

```sh
tmux set-option -u -p -t "$TMUX_PANE" @agent-state
```

Read (visualiser) — folded into the existing `list-panes` call, no extra
polling:

```sh
tmux list-panes -a -F '#{pane_id}|#{@agent-state}'
# %287|{"tool":"pi","state":"waiting","ts":1718...}
# %447|        <- empty when no adapter / cleared
```

Why this channel: pane-scoped (one value per pane, isolated), lives in the tmux
server the reader already talks to, zero extra file/socket/IPC, and survives
adapter crashes as a stale value the reader can detect.

## Payload

A single-line JSON string (no tabs, no newlines) stored as the option value:

```jsonc
{
  "tool": "pi",            // required: "pi" | "claude" | "codex" | <short id>
  "state": "waiting",      // required: "waiting" | "busy"
  "ts": 1718000000.5,      // required: epoch seconds of THIS write (float ok)
  "since": 1718000000.0,   // optional: epoch seconds when current state began
  "detail": "done"       // optional: short human hint, e.g. "working" | "done" | "ready"
}
```

- `state` is the only thing the reader strictly needs besides `ts`.
- `tool` lets the reader label the pane without walking the process tree.
- `ts` is mandatory for staleness (see below). Write it on every write.
- `detail` is mostly a **display hint** (`ready` / `working` / `done`);
  readers MUST NOT use it to decide whether a pane needs attention — except
  `detail=asking`, which is reliable: interactive tools (e.g. pi's
  `question` extension) write `waiting` + `detail=asking` while they block
  on user input, because the tool itself knows it is waiting. Writers that
  do not actually block must never emit `asking`.

Reliability: `state` and `asking` are driven by deterministic facts only
(events + the blocking tool's own knowledge). Nothing is inferred from tool
names or screen content.

## Writer rules

1. **Write on state transitions only** (waiting→busy, busy→waiting). Do not
   write on every token / tool update — that would spam `tmux`.
2. **Heartbeat**: re-write the current state every ~15s even if unchanged, so
   `ts` stays fresh and the reader knows the adapter is alive.
3. **Clear on shutdown** (`set-option -u -p`) so stale data does not linger
   after the agent exits and the shell returns.
4. If `$TMUX_PANE` is unset (not running inside tmux), the adapter is a no-op.

## Reader rules (precedence)

For each pane, the reader decides the final state in this order:

1. `pane_dead == 1` → **dead** (always wins).
2. `@agent-state` present and `now - ts <= STALE` (default 45s) → use the
   adapter's `state` (and `tool`/`detail` for display).
3. `@agent-state` present but older than STALE → **stale** (adapter lost its
   heartbeat; shown as-is, no guessing).
4. no `@agent-state` and `pane_current_command` is a shell → **waiting**
   (tmux fact, exact).
5. otherwise → **untracked** (no adapter on this pane; reported honestly).

`STALE` = 3× the heartbeat interval, so three missed heartbeats mark the
adapter as lost. There is no heuristic fallback: panes without a live
adapter are `stale`/`untracked`, never inferred.

## Reference values

| constant                | default | meaning                          |
| ----------------------- | ------- | -------------------------------- |
| heartbeat interval      | 15 s    | adapter re-write cadence         |
| STALE (reader)          | 45 s    | demote adapter to `stale`     |
| option name             | `@agent-state` | tmux pane user option     |
| pane id env (writer)    | `$TMUX_PANE` | tmux-provided, per-pane      |

## Last interaction I/O (`@agent-io`)

Optional second pane option carrying the **last** user input and assistant
output of the most recent turn, so a viewer can show just that instead of
the whole scrollback.

```sh
tmux set-option -p -t "$TMUX_PANE" @agent-io '<json>'
```

Payload (single-line JSON):

```jsonc
{ "input": "hi", "output": "OK", "ts": 1718... }
```

- written by the adapter at turn end (`agent_settled` / `Stop`), and refreshed
  with `output: ""` at the start of a new turn so the viewer can show
  "input submitted, output pending".
- `input` is the raw user text (pre skill/template expansion); `output` is the
  final assistant text (tool-call blocks excluded). Both are truncated
  (~500 / ~4000 chars by default).
- same staleness/precedence idea: reader trusts it when fresh, else falls back
  to the raw captured terminal.
- only pi emits this so far (it has the `input` text + `message_end` content).
  claude/codex hooks can fill it from their session/transcript if desired.

## Per-tool adapters

| tool        | adapter form                         | events / hooks that map to states                         |
| ----------- | ------------------------------------ | --------------------------------------------------------- |
| **pi**      | TypeScript extension (this repo)     | `input`/`agent_start`→busy, `agent_settled`→waiting |
| **claude**  | `~/.claude/settings.json` hooks      | `Notification`/`Stop`→waiting, `UserPromptSubmit`/`PreToolUse`→busy |
| **codex**   | `~/.codex/hooks.json` hooks          | `approval-requested`/`Stop`→waiting, `UserPromptSubmit`→busy |
| **zsh**     | none — `pane_current_command=zsh` ⇒ waiting (tmux fact, exact) | |

All adapters write the **same** `@agent-state` payload, so the reader is
tool-agnostic: it just trusts the freshest writer on the pane.
