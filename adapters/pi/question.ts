/**
 * OPTIONAL example: a blocking "question" tool that reports waiting/asking
 * to tmux-agent-state while it waits for the user (see ../../PROTOCOL.md).
 *
 * agent-state.ts works without this file — a pi blocked on a question simply
 * shows busy. Install this only if you want the agent to show up as asking
 * (needs-input) in the status bar while a question is open.
 *
 * Requires agent-state.ts (it owns the initial state and shutdown cleanup;
 * this file only restores busy/working after the question closes).
 *
 * This is also the reference for the "asking" contract — to add asking
 * reporting to your own blocking tool, wrap the blocking call like this:
 *
 *   1. before blocking: set globalThis.__tmuxPanelQuestion =
 *      { active: true, since: Date.now() } and write waiting/asking;
 *   2. in a finally block: clear the flag, restore busy/working.
 *
 * agent-state.ts reads the same flag in writeState(), so its own transition
 * writes do not clobber waiting/asking mid-question.
 *
 * Load (after agent-state.ts): symlink into ~/.pi/agent/extensions/, /reload.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { Type } from "typebox";

const QUESTION_FLAG = "__tmuxPanelQuestion";

function setQuestionFlag(active: boolean): void {
  (globalThis as Record<string, unknown>)[QUESTION_FLAG] = active
    ? { active: true as const, since: Date.now() }
    : undefined;
}

function writeAgentState(state: "waiting" | "busy", detail: string): void {
  const pane = process.env.TMUX_PANE;
  if (!pane) return; // not inside tmux -> no-op
  const payload = JSON.stringify({
    tool: "pi",
    state,
    ts: Date.now() / 1000,
    since: Date.now() / 1000,
    detail,
  });
  const p = spawn(
    "tmux",
    ["set-option", "-p", "-t", pane, "@agent-state", payload],
    { stdio: "ignore" },
  );
  p.unref();
  p.on("error", () => {});
}

const Params = Type.Object({
  question: Type.String({ description: "The question to ask the user" }),
  options: Type.Array(Type.String(), {
    description: "Options for the user to choose from",
  }),
});

export default function question(pi: ExtensionAPI): void {
  pi.registerTool({
    name: "question",
    label: "Question",
    description:
      "Ask the user a question and let them pick from options. Use when you need user input to proceed.",
    parameters: Params,

    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      if (params.options.length === 0) {
        return {
          content: [{ type: "text", text: "Error: no options provided" }],
          details: null,
        };
      }

      setQuestionFlag(true);
      writeAgentState("waiting", "asking");
      try {
        const answer = await ctx.ui.select(params.question, params.options);
        const text =
          answer === undefined
            ? "User cancelled the question"
            : `User selected: ${answer}`;
        return { content: [{ type: "text", text }], details: null };
      } finally {
        setQuestionFlag(false);
        writeAgentState("busy", "working");
      }
    },
  });
}
