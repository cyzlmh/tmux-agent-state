/**
 * pi extension: report agent state + last interaction I/O to tmux-agent-state.
 *
 * Writes two tmux pane-scoped user options (see ../PROTOCOL.md):
 *   @agent-state  {tool, state, ts, since, detail}     (waiting/busy, on transitions only)
 *   @agent-io      {input, output, ts}                  (last user input + last assistant output, per turn)
 *
 * Load (dev):   pi --extension ./adapters/pi/agent-state.ts   (from the repo root)
 * Load (installed): symlink into ~/.pi/agent/extensions/ then /reload
 *
 * Config (env):
 *   TMUX_STATUS_IO_MAX_OUT    max chars of captured output (default 4000)
 *   TMUX_STATUS_IO_MAX_IN     max chars of captured input  (default 500)
 *
 * Reliability: state is driven only by deterministic events (input /
 * agent_start -> busy, agent_settled -> waiting). There is deliberately NO
 * tool-name guessing. "Agent is asking" comes from pi's own signals:
 * ui_prompt_start/ui_prompt_end (pi >= 0.84.4) fire around every blocking
 * ctx.ui prompt, and the shared __tmuxPanelQuestion flag covers the same
 * ground for older pi versions (see question.ts). detail is a display hint
 * only (ready/working/done, plus bg while bg-tasks has running jobs). A turn
 * that ended without finishing — the model hit its output limit (stopReason
 * "length") or the run failed (stopReason "error") — reports
 * detail=truncated / detail=error, which readers render as an error rather
 * than a plain done.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Where this extension really lives. jiti loads it as CJS and does NOT
// resolve symlinks, so when installed via ~/.pi/agent/extensions/agent-state.ts
// (a symlink) __dirname would point at the install dir — realpath fixes that
// so relative paths (../../statusbar/...) resolve into the repo.
const EXT_DIR: string = path.dirname(
  fs.realpathSync(
    typeof __filename !== "undefined"
      ? __filename
      : fileURLToPath(import.meta.url),
  ),
);

// Optional: colorize.sh applies pane-border / window-title colors from
// @agent-state. Only spawned on state *transitions*. Set TMUX_STATUS_COLORIZE
// to a different path, or to an empty string to disable coloring.
const COLORIZE = process.env.TMUX_STATUS_COLORIZE !== undefined
    ? process.env.TMUX_STATUS_COLORIZE
    : path.join(EXT_DIR, "..", "..", "statusbar", "scripts", "colorize.sh");
const OPTION = "@agent-state";
const OPTION_IO = "@agent-io";
const TOOL = "pi";
const MAX_IN = Number(process.env.TMUX_STATUS_IO_MAX_IN ?? 500);
const MAX_OUT = Number(process.env.TMUX_STATUS_IO_MAX_OUT ?? 4000);

type State = "waiting" | "busy";

// Shared flag with the question tool extension (same pi process): while the
// question tool blocks on user input it sets __tmuxPanelQuestion and writes
// waiting/asking; writeState reflects that instead of our in-memory busy.
// Superseded by ui_prompt_start/ui_prompt_end below, but kept for pi < 0.84.4
// and for tools that want to report asking on their own terms.
type QuestionFlag = { active: true; since: number } | undefined;

function questionFlag(): QuestionFlag {
  return (globalThis as Record<string, unknown>).__tmuxPanelQuestion as QuestionFlag;
}

// Shared flag with the bg-tasks extension (same pi process): it publishes
// the number of running background tasks as __piBgTasksRunning (undefined
// when zero) and pokes __tmuxAgentStateRefresh on count changes. While
// waiting with tasks in flight we report detail=bg, so tmux can tell
// "idle with background work" apart from a plain done.
function bgRunning(): number {
  return (
    ((globalThis as Record<string, unknown>).__piBgTasksRunning as
      | number
      | undefined) ?? 0
  );
}

/** Pull plain text out of an assistant message's content (string | content blocks). */
function extractText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  const parts: string[] = [];
  for (const b of content) {
    if (typeof b === "string") {
      parts.push(b);
      continue;
    }
    const blk = b as Record<string, unknown>;
    if (blk.type === "tool_use") continue; // skip tool-call blocks
    if (typeof blk.text === "string") parts.push(blk.text);
  }
  return parts.join("\n").trim();
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n) + "…";
}

/**
 * Last non-empty assistant text in the session, walking entries backwards.
 * More reliable than tracking message_end events: it reflects the final
 * answer rather than whichever assistant message happened to finalize last
 * with text (tool-call blocks are skipped). Best-effort: if the turn ended
 * with a pure tool call, this returns the previous turn's text.
 */
function lastAssistantText(entries: unknown[]): string {
  for (let i = entries.length - 1; i >= 0; i--) {
    const e = entries[i] as
      | { type?: string; message?: { role?: string; content?: unknown } }
      | undefined;
    if (e?.type !== "message" || e.message?.role !== "assistant") continue;
    const t = extractText(e.message.content);
    if (t) return t;
  }
  return "";
}

/**
 * The assistant message that ended the run, or undefined. Unlike
 * lastAssistantText this does not skip tool-call-only messages: the last
 * assistant message is the one whose stopReason pi surfaces in its own UI
 * ("Response was truncated before completion." / "Error: …").
 */
function lastAssistantMessage(
  entries: unknown[],
): { stopReason?: string } | undefined {
  for (let i = entries.length - 1; i >= 0; i--) {
    const e = entries[i] as
      | { type?: string; message?: { role?: string; stopReason?: string } }
      | undefined;
    if (e?.type !== "message" || e.message?.role !== "assistant") continue;
    return e.message;
  }
  return undefined;
}

/**
 * detail for a turn that just settled. A turn that stopped at the model's
 * output limit ("length") or failed ("error") never actually finished, so it
 * must not read as a plain done. "aborted" is deliberately done: the user
 * cancelled on purpose, which is not a fault to flag.
 */
function settledDetail(stopReason: string | undefined): string {
  if (stopReason === "length") return "truncated";
  if (stopReason === "error") return "error";
  return "done";
}

export default function agentState(pi: ExtensionAPI): void {
  const envPane = process.env.TMUX_PANE;
  if (!envPane) return; // not running inside tmux -> no-op
  const pane: string = envPane;

  let state: State | null = null;
  let detail = "";
  let since = Date.now();

  // Depth of blocking ctx.ui prompts (pi emits ui_prompt_start/_end around
  // each one). > 0 means pi is sitting in a dialog waiting for the user, which
  // is exactly the needs-input case, whatever tool raised it.
  let uiPrompts = 0;
  let uiPromptSince = 0;

  // last interaction I/O
  let lastInput = "";
  let lastOutput = "";

  function tmux(args: string[]): void {
    const p = spawn("tmux", args, { stdio: "ignore" });
    p.unref();
    p.on("error", () => {});
  }

  function writeState(): void {
    if (!state) return;
    const q = questionFlag();
    // A blocking user-facing prompt (pi's ui_prompt_* or the question tool's
    // flag) is reported as waiting/asking instead of our in-memory busy.
    const asking = q?.active === true || uiPrompts > 0;
    const s: State = asking ? "waiting" : state;
    // Precedence: a blocking prompt beats everything; then a turn that ended
    // badly (truncated/error) — background jobs must not mask a failure; then
    // bg while waiting with work in flight; then the plain detail.
    const d = asking
      ? "asking"
      : detail === "truncated" || detail === "error"
        ? detail
        : s === "waiting" && bgRunning() > 0
          ? "bg"
          : detail;
    const sn = asking ? (q?.since ?? uiPromptSince ?? since) : since;
    const payload = JSON.stringify({
      tool: TOOL,
      state: s,
      ts: Date.now() / 1000,
      since: sn / 1000,
      detail: d,
    });
    tmux(["set-option", "-p", "-t", pane, OPTION, payload]);
  }

  function writeIo(): void {
    const payload = JSON.stringify({
      input: truncate(lastInput, MAX_IN),
      output: truncate(lastOutput, MAX_OUT),
      ts: Date.now() / 1000,
    });
    tmux(["set-option", "-p", "-t", pane, OPTION_IO, payload]);
  }

  // Border/title coloring is a consumer of @agent-state; spawn it on real
  // transitions (not heartbeats). Failures are ignored (colorize is optional).
  function colorize(): void {
    if (!COLORIZE) return;
    const p = spawn("bash", [COLORIZE, pane], { stdio: "ignore" });
    p.unref();
    p.on("error", () => {});
  }

  function set(next: State, d: string): void {
    if (state === next && detail === d) return;
    if (state !== next) {
      state = next;
      since = Date.now();
    }
    detail = d;
    writeState();
    colorize();
  }

  function clear(): void {
    state = null;
    detail = "";
    uiPrompts = 0;
    lastInput = "";
    lastOutput = "";
    (globalThis as Record<string, unknown>).__tmuxAgentStateRefresh = undefined;
    tmux(["set-option", "-u", "-p", "-t", pane, OPTION]);
    tmux(["set-option", "-u", "-p", "-t", pane, OPTION_IO]);
  }

  // --- state (deterministic events only) ---

  // bg-tasks pokes this when its running-task count changes (e.g. a task
  // finishes while the agent sits waiting) so the state is re-written
  // immediately instead of waiting for the next turn transition.
  (globalThis as Record<string, unknown>).__tmuxAgentStateRefresh = () => {
    writeState();
    colorize();
  };

  pi.on("session_start", () => {
    set("waiting", "ready");
  });

  // user submitted input -> working; capture the input text for @agent-io
  pi.on("input", (event) => {
    lastInput = event.text ?? "";
    lastOutput = ""; // new turn: previous output is stale
    writeIo();
    set("busy", "working");
  });
  pi.on("before_agent_start", () => set("busy", "working"));
  pi.on("agent_start", () => set("busy", "working"));

  // turn fully done (no auto-retry / compaction / queued follow-up pending)
  // -> waiting; publish final I/O for this interaction. Output comes from the
  // settled session entries, not from streaming events. A turn cut off by the
  // output limit, or one that failed, is reported as truncated/error instead
  // of done so a reader can flag it.
  pi.on("agent_settled", (_event, ctx) => {
    const entries = ctx.sessionManager.getEntries();
    lastOutput = lastAssistantText(entries);
    set("waiting", settledDetail(lastAssistantMessage(entries)?.stopReason));
    writeIo();
  });

  // pi fires these around every blocking ctx.ui prompt (select/confirm/input/
  // editor/custom), including prompts raised by other extensions — so a pane
  // parked on a permission dialog reads needs-input without any tool-name
  // guessing. They are notifications, not transitions: writeState() is called
  // directly because the in-memory state (busy) does not change here.
  pi.on("ui_prompt_start", () => {
    if (uiPrompts++ === 0) {
      uiPromptSince = Date.now();
      writeState();
      colorize();
    }
  });

  pi.on("ui_prompt_end", () => {
    if (uiPrompts > 0 && --uiPrompts === 0) {
      writeState();
      colorize();
    }
  });

  pi.on("session_shutdown", () => clear());
}
