/**
 * pi extension: report agent state + last interaction I/O to tmux-agent-state.
 *
 * Writes two tmux pane-scoped user options (see ../PROTOCOL.md):
 *   @agent-state  {tool, state, ts, since, detail}     (waiting/busy, on transition + heartbeat)
 *   @agent-io      {input, output, ts}                  (last user input + last assistant output, per turn)
 *
 * Load (dev):   pi --extension ./pi/agent-state.ts   (from the repo root)
 * Load (installed): symlink into ~/.pi/agent/extensions/ then /reload
 *
 * Config (env):
 *   TMUX_PANEL_IO_MAX_OUT    max chars of captured output (default 4000)
 *   TMUX_PANEL_IO_MAX_IN     max chars of captured input  (default 500)
 *
 * Reliability: state is driven only by deterministic events (input /
 * agent_start -> busy, agent_settled -> waiting). There is deliberately NO
 * tool-name guessing: pi exposes no event for "UI waiting for user input",
 * so an agent blocked on a question tool reports busy until the turn
 * settles. detail is a display hint only (ready/working/done).
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Where this extension really lives. jiti loads it as CJS and does NOT
// resolve symlinks, so when installed via ~/.pi/agent/extensions/agent-state.ts
// (a symlink) __dirname would point at the install dir — realpath fixes that
// so relative paths (../panel/...) resolve into the repo.
const EXT_DIR: string = path.dirname(
  fs.realpathSync(
    typeof __filename !== "undefined"
      ? __filename
      : fileURLToPath(import.meta.url),
  ),
);

// Optional: colorize.sh applies pane-border / window-title colors from
// @agent-state. Only spawned on state *transitions*. Set TMUX_PANEL_COLORIZE
// to a different path, or to an empty string to disable coloring.
const COLORIZE = process.env.TMUX_PANEL_COLORIZE !== undefined
    ? process.env.TMUX_PANEL_COLORIZE
    : path.join(EXT_DIR, "..", "panel", "scripts", "colorize.sh");
const OPTION = "@agent-state";
const OPTION_IO = "@agent-io";
const TOOL = "pi";
const HEARTBEAT_MS = 15_000;
const MAX_IN = Number(process.env.TMUX_PANEL_IO_MAX_IN ?? 500);
const MAX_OUT = Number(process.env.TMUX_PANEL_IO_MAX_OUT ?? 4000);

type State = "waiting" | "busy";

// Shared flag with the question tool extension (same pi process): while the
// question tool blocks on user input it sets __tmuxPanelQuestion and writes
// waiting/asking; our heartbeat must preserve that instead of overwriting it
// with busy (ts keeps refreshing, so the reader sees a live "asking").
type QuestionFlag = { active: true; since: number } | undefined;

function questionFlag(): QuestionFlag {
  return (globalThis as Record<string, unknown>).__tmuxPanelQuestion as QuestionFlag;
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

export default function agentState(pi: ExtensionAPI): void {
  const envPane = process.env.TMUX_PANE;
  if (!envPane) return; // not running inside tmux -> no-op
  const pane: string = envPane;

  let state: State | null = null;
  let detail = "";
  let since = Date.now();
  let hb: ReturnType<typeof setInterval> | null = null;

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
    // While the question tool is waiting for the user, report waiting/asking
    // (its since) instead of our in-memory busy state.
    const s: State = q?.active ? "waiting" : state;
    const d = q?.active ? "asking" : detail;
    const sn = q?.active && q.since ? q.since : since;
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

  function startHeartbeat(): void {
    if (hb) return;
    hb = setInterval(writeState, HEARTBEAT_MS);
    hb.unref();
  }

  function clear(): void {
    if (hb) {
      clearInterval(hb);
      hb = null;
    }
    state = null;
    detail = "";
    lastInput = "";
    lastOutput = "";
    tmux(["set-option", "-u", "-p", "-t", pane, OPTION]);
    tmux(["set-option", "-u", "-p", "-t", pane, OPTION_IO]);
  }

  // --- state (deterministic events only) ---

  pi.on("session_start", () => {
    startHeartbeat();
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
  // settled session entries, not from streaming events.
  pi.on("agent_settled", (_event, ctx) => {
    lastOutput = lastAssistantText(ctx.sessionManager.getEntries());
    set("waiting", "done");
    writeIo();
  });

  pi.on("session_shutdown", () => clear());
}
