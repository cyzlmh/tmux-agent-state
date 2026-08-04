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

An adapter writes its state; the reader trusts it while the adapter's
process is alive, and reports `stale` explicitly once the process is gone.
Explicit is better than implicit: no screen-scraping, and liveness is a
fact (the pane foreground command), not a guess.

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
tmux list-panes -a -F '#{pane_id}|#{pane_dead}|#{pane_current_command}|#{@agent-state}'
# %287|0|pi|{"tool":"pi","state":"waiting","ts":1718...}
# %447|0|zsh|        <- empty when no adapter / cleared
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
  "ts": 1718000000.5,      // required: epoch seconds of THIS write (float ok; display only, never liveness)
  "since": 1718000000.0,   // optional: epoch seconds when current state began
  "detail": "done"       // optional: short human hint, e.g. "working" | "done" | "ready"
}
```

- `state` is the only thing the reader strictly needs.
- `tool` lets the reader label the pane without walking the process tree.
- `ts` must be written on every write, but is **informational only**
  (last-write time, for display such as "last updated X min ago") — readers
  MUST NOT use it for liveness. Liveness is decided by process presence, not
  by ts: adapters do not heartbeat (see Writer rules), so a long-idle agent
  would otherwise be misreported stale.
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
2. **No heartbeat.** The reader determines liveness itself: while the agent
   process occupies the pane (foreground is not a shell), the last written
   state is trusted regardless of age — an idle agent waiting for input
   never goes stale. (Hook-based adapters have no way to run a periodic
   re-write anyway; requiring one would force a background process per
   agent.)
3. **Clear on shutdown** (`set-option -u -p`) so stale data does not linger
   after the agent exits and the shell returns.
4. If `$TMUX_PANE` is unset (not running inside tmux), the adapter is a no-op.

## Reader rules (precedence)

For each pane, the reader decides the final state in this order:

1. `pane_dead == 1` → **dead** (always wins).
2. `@agent-state` present and `pane_current_command` is **not** a shell →
   use the adapter's `state` (and `tool`/`detail` for display). The process
   is alive, so the state is live even if written minutes ago.
3. `@agent-state` present but `pane_current_command` is a shell → **stale**
   (adapter process gone; reported as-is, no guessing).
4. no `@agent-state` and `pane_current_command` is a shell → **waiting**
   (tmux fact, exact).
5. otherwise → **untracked** (no adapter on this pane; reported honestly).

Liveness is a tmux fact (`pane_current_command` is the foreground process),
so a killed adapter shows `stale` immediately rather than after N missed
heartbeats. There is no heuristic fallback: panes without a live adapter
are `stale`/`untracked`, never inferred.

## Reference values

| constant                | default | meaning                          |
| ----------------------- | ------- | -------------------------------- |
| option name             | `@agent-state` | tmux pane user option     |
| pane id env (writer)    | `$TMUX_PANE` | tmux-provided, per-pane      |

(There is no heartbeat interval or staleness threshold: liveness is decided
by the pane foreground command, see Reader rules.)

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
- same liveness idea as `@agent-state`: the reader trusts it while the agent
  process occupies the pane (foreground is not a shell), else falls back to
  the raw captured terminal.
- only pi emits this so far (it has the `input` text + `message_end` content).
  claude/codex hooks can fill it from their session/transcript if desired.

## Per-tool adapters

| tool        | adapter form                         | events / hooks that map to states                         |
| ----------- | ------------------------------------ | --------------------------------------------------------- |
| **pi**      | TypeScript extension (this repo)     | `input`/`agent_start`→busy, `agent_settled`→waiting, blocking tool→waiting/asking |
| **claude**  | `~/.claude/settings.json` hooks      | `UserPromptSubmit`/`PreToolUse`→busy, `PermissionRequest`/`Elicitation`→waiting/asking, `Stop`→waiting/done, `SessionStart`→ready, `SessionEnd`→clear |
| **codex**   | `~/.codex/hooks.json` hooks          | `UserPromptSubmit`/`PreToolUse`→busy, `PermissionRequest`→waiting/asking, `Stop`→waiting/done, `SessionStart`→ready, `SessionEnd`→clear |
| **zsh**     | none — `pane_current_command=zsh` ⇒ waiting (tmux fact, exact) | |

All adapters write the **same** `@agent-state` payload, so the reader is
tool-agnostic: it just trusts the freshest writer on the pane.
