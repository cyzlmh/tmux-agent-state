/**
 * Tests for adapters/pi/agent-state.ts — the pi extension's state machine.
 *
 * The extension only imports *types* from pi, so it runs under plain bun with a
 * fake ExtensionAPI and a stub `tmux` on PATH. Every tmux invocation is logged
 * and the assertions read the last @agent-state write, which is exactly what a
 * reader would see.
 *
 *   bun tests/test-pi-adapter.ts
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// --- harness -----------------------------------------------------------------

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "tas-pi-"));
const STUB = path.join(TMP, "bin");
const CALLS = path.join(TMP, "calls.log");
fs.mkdirSync(STUB, { recursive: true });
fs.writeFileSync(
  path.join(STUB, "tmux"),
  `#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$TAS_CALLS"
exit 0
`,
  { mode: 0o755 },
);
fs.writeFileSync(CALLS, "");

process.env.PATH = `${STUB}:${process.env.PATH}`;
process.env.TAS_CALLS = CALLS;
process.env.TMUX_PANE = "%1";
process.env.TMUX_STATUS_COLORIZE = ""; // colorize.sh off: it needs a real tmux
process.env.TMUX_AGENT_STATE_LOG = "";

const PANE = "%1";

let failures = 0;
function check(ok: boolean, what: string): void {
  if (ok) {
    console.log(`  ok   ${what}`);
  } else {
    console.log(`  FAIL ${what}`);
    failures++;
  }
}

/** Last payload written to @agent-state, or null when the option was cleared. */
function lastState(): Record<string, unknown> | null {
  const lines = fs.readFileSync(CALLS, "utf8").split("\n").filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i];
    if (!line.includes("@agent-state")) continue;
    if (line.includes("-u")) return null; // cleared
    const m = line.match(/\{.*\}$/);
    if (!m) continue;
    return JSON.parse(m[0]) as Record<string, unknown>;
  }
  return null;
}

/** Drop logged calls so the next assertion only sees fresh writes. */
function resetCalls(): void {
  fs.writeFileSync(CALLS, "");
}

/** tmux is spawned detached; give it a moment to land in the log. */
async function settle(): Promise<void> {
  for (let i = 0; i < 50; i++) {
    await new Promise((r) => setTimeout(r, 10));
    if (fs.readFileSync(CALLS, "utf8").length > 0) {
      await new Promise((r) => setTimeout(r, 20));
      return;
    }
  }
}

// --- fake ExtensionAPI -------------------------------------------------------

type Handler = (event: any, ctx?: any) => unknown;
const handlers = new Map<string, Handler[]>();

const fakePi = {
  on(event: string, handler: Handler) {
    const list = handlers.get(event) ?? [];
    list.push(handler);
    handlers.set(event, list);
    return () => {};
  },
} as any;

async function fire(event: string, payload: any = {}): Promise<void> {
  for (const h of handlers.get(event) ?? []) {
    await h({ type: event, ...payload }, fakeCtx);
  }
  await settle();
}

const fakeCtx = {
  sessionManager: {
    getEntries: () => [
      {
        type: "message",
        message: { role: "assistant", content: [{ type: "text", text: "done!" }] },
      },
    ],
  },
} as any;

/** Rebuild fakeCtx so agent_settled sees an assistant message with `stopReason`. */
function setStopReason(stopReason: string | undefined, text = "partial output"): void {
  fakeCtx.sessionManager.getEntries = () => [
    {
      type: "message",
      message: {
        role: "assistant",
        stopReason,
        content: text ? [{ type: "text", text }] : [],
      },
    },
  ];
}

/**
 * The shape a real truncated turn leaves behind (seen in ~/.pi/agent/sessions):
 * the final assistant message is `stopReason: "length"` with EMPTY text (the
 * model produced only thinking/tool calls before hitting the output cap), after
 * earlier tool-call turns. `lastAssistantText` walks back for display text while
 * the stop reason still comes from the last message.
 */
function setTruncatedHistory(): void {
  fakeCtx.sessionManager.getEntries = () => [
    {
      type: "message",
      message: {
        role: "assistant",
        stopReason: "toolUse",
        content: [{ type: "text", text: "earlier answer" }],
      },
    },
    {
      type: "message",
      message: { role: "toolResult", content: [{ type: "text", text: "ok" }] },
    },
    {
      type: "message",
      message: { role: "assistant", stopReason: "length", content: [] },
    },
  ];
}

// --- run ---------------------------------------------------------------------

const mod = await import("../adapters/pi/agent-state.ts");
mod.default(fakePi);

const G = globalThis as Record<string, unknown>;

console.log("pi adapter: state machine");

// 1. session_start -> waiting/ready
await fire("session_start", { reason: "startup" });
let s = lastState();
check(s?.state === "waiting" && s?.detail === "ready", "session_start -> waiting/ready");
check(s?.tool === "pi", "payload carries tool=pi");
check(typeof s?.ts === "number", "payload carries a numeric ts");

// 2. input -> busy/working, and @agent-io is refreshed
resetCalls();
await fire("input", { text: "hi", source: "interactive" });
s = lastState();
check(s?.state === "busy" && s?.detail === "working", "input -> busy/working");
check(
  fs.readFileSync(CALLS, "utf8").includes("@agent-io"),
  "input refreshes @agent-io",
);

// 3. a blocking ctx.ui prompt -> waiting/asking (the new ui_prompt_* path)
resetCalls();
await fire("ui_prompt_start", { kind: "confirm", title: "Allow?" });
s = lastState();
check(
  s?.state === "waiting" && s?.detail === "asking",
  "ui_prompt_start -> waiting/asking",
);

// 4. ...and the prompt closing restores the in-memory busy state
resetCalls();
await fire("ui_prompt_end", { kind: "confirm" });
s = lastState();
check(s?.state === "busy" && s?.detail === "working", "ui_prompt_end -> busy/working");

// 5. nested prompts: the inner close must not clear asking
resetCalls();
await fire("ui_prompt_start", { kind: "select" });
await fire("ui_prompt_start", { kind: "input" });
s = lastState();
check(s?.detail === "asking", "nested ui prompts -> asking");
resetCalls();
await fire("ui_prompt_end", { kind: "input" });
check(
  !fs.readFileSync(CALLS, "utf8").includes("@agent-state"),
  "closing an inner prompt writes nothing (no redundant tmux churn)",
);
await fire("ui_prompt_end", { kind: "select" });
s = lastState();
check(s?.detail === "working", "outer ui prompt close restores working");

// 6. the question tool's shared flag still reports asking (pi < 0.84.4 path).
// The bg-tasks refresh hook is the realistic way to force a re-read while the
// in-memory state (busy) is unchanged.
resetCalls();
G.__tmuxPanelQuestion = { active: true, since: Date.now() };
(G.__tmuxAgentStateRefresh as () => void)();
await settle();
s = lastState();
check(s?.detail === "asking", "question flag -> asking");
check(
  typeof s?.since === "number" && (s.since as number) <= (s.ts as number),
  "asking since comes from the prompt start",
);
resetCalls();
G.__tmuxPanelQuestion = undefined;
(G.__tmuxAgentStateRefresh as () => void)();
await settle();
s = lastState();
check(s?.detail === "working", "clearing the question flag restores working");

// 7. bg-tasks: idle + running background jobs -> waiting/bg
resetCalls();
G.__piBgTasksRunning = 2;
await fire("agent_settled");
s = lastState();
check(s?.state === "waiting" && s?.detail === "bg", "agent_settled + bg tasks -> waiting/bg");
check(
  fs.readFileSync(CALLS, "utf8").includes("@agent-io"),
  "agent_settled publishes final @agent-io",
);
G.__piBgTasksRunning = 0;

// 8. dropping the background count returns to waiting/done
resetCalls();
G.__piBgTasksRunning = 0;
(G.__tmuxAgentStateRefresh as () => void)();
await settle();
s = lastState();
check(s?.state === "waiting" && s?.detail === "done", "bg tasks finish -> waiting/done");

// 8b. a turn cut off by the output limit must not read as a plain done
resetCalls();
setStopReason("length");
await fire("agent_settled");
s = lastState();
check(s?.detail === "truncated", "stopReason=length -> detail=truncated");
check(s?.state === "waiting", "truncated turn is still waiting (idle)");

// 8c. a failed turn is its own state
resetCalls();
setStopReason("error");
await fire("agent_settled");
s = lastState();
check(s?.detail === "error", "stopReason=error -> detail=error");

// 8d. a user abort is deliberate, not a fault -> plain done
resetCalls();
setStopReason("aborted");
await fire("agent_settled");
s = lastState();
check(s?.detail === "done", "stopReason=aborted -> detail=done (not an error)");

// 8e. truncated/error are not masked by background jobs
resetCalls();
G.__piBgTasksRunning = 1;
setStopReason("length");
await fire("agent_settled");
s = lastState();
check(s?.detail === "truncated", "bg tasks must not mask a truncated turn");
// the bg-tasks poke republishes the in-memory detail, which is still the fault
resetCalls();
(G.__tmuxAgentStateRefresh as () => void)();
await settle();
s = lastState();
check(s?.detail === "truncated", "bg refresh must not mask a truncated turn");
resetCalls();
G.__piBgTasksRunning = 0;
(G.__tmuxAgentStateRefresh as () => void)();
await settle();
s = lastState();
check(s?.detail === "truncated", "dropping bg tasks keeps the truncated detail");

// 8f. a normal completion is still done, and a new turn clears the fault
resetCalls();
setStopReason("stop");
await fire("agent_settled");
s = lastState();
check(s?.detail === "done", "stopReason=stop -> detail=done");
resetCalls();
setStopReason("length");
await fire("agent_settled");
resetCalls();
await fire("input", { text: "retry", source: "interactive" });
s = lastState();
check(s?.detail === "working", "a new turn clears a previous truncated detail");

// 8g. realistic truncation: the final assistant message is empty (thinking or
//     tool calls only), so the fault must come from the stop reason, not from
//     there being no text.
resetCalls();
setTruncatedHistory();
await fire("agent_settled");
s = lastState();
check(
  s?.detail === "truncated",
  "empty final message + stopReason=length -> detail=truncated",
);
check(
  fs.readFileSync(CALLS, "utf8").includes("earlier answer"),
  "@agent-io still shows the last non-empty text on a truncated turn",
);

// 9. the bg-tasks refresh poke rewrites the current state (a plain done turn)
resetCalls();
setStopReason("stop");
await fire("agent_settled");
resetCalls();
G.__piBgTasksRunning = 1;
(G.__tmuxAgentStateRefresh as () => void)();
await settle();
s = lastState();
check(s?.detail === "bg", "__tmuxAgentStateRefresh republishes detail=bg");
G.__piBgTasksRunning = 0;

// 10. session_shutdown clears the option (and the refresh hook)
resetCalls();
await fire("session_shutdown", { reason: "quit" });
check(lastState() === null, "session_shutdown clears @agent-state");
check(G.__tmuxAgentStateRefresh === undefined, "session_shutdown drops the refresh hook");

fs.rmSync(TMP, { recursive: true, force: true });

if (failures > 0) {
  console.log(`FAIL: ${failures} check(s)`);
  process.exit(1);
}
console.log("PASS: pi adapter state machine");
