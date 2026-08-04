/**
 * Question tool with a full custom TUI (options list + inline editor).
 *
 * Reference implementation of the "asking" contract for tmux-agent-state
 * (PROTOCOL.md): while this tool blocks on the user it reports
 * `waiting` + `detail=asking` to the @agent-state pane option, then restores
 * `busy` + `working` in a `finally` block. agent-state.ts has NO heartbeat
 * (it writes on state transitions only), so a blocking tool must write
 * waiting/asking itself before blocking — this is reliable, the tool itself
 * knows it is waiting for the user.
 *
 * Requires agent-state.ts (it owns the initial state and shutdown cleanup;
 * this file only restores busy/working after the question closes).
 *
 * To add asking reporting to your own blocking tool, wrap the blocking call
 * like this:
 *   1. before blocking: set globalThis.__tmuxPanelQuestion =
 *      { active: true, since: Date.now() } and write waiting/asking;
 *   2. in a finally block: clear the flag, restore busy/working.
 * agent-state.ts reads the same flag in writeState(), so its own transition
 * writes do not clobber waiting/asking mid-question.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
	Editor,
	type EditorTheme,
	Key,
	matchesKey,
	Text,
	visibleWidth,
	wrapTextWithAnsi,
} from "@earendil-works/pi-tui";
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Type } from "typebox";

interface OptionWithDesc {
	label: string;
	description?: string;
}

type DisplayOption = OptionWithDesc & { isOther?: boolean };

interface QuestionDetails {
	question: string;
	options: string[];
	answer: string | null;
	wasCustom?: boolean;
}

// --- tmux-agent-state state reporting ----------------------------------------
//
// While this tool blocks waiting for user input, report `waiting/asking` to
// tmux-agent-state via the @agent-state pane option (see PROTOCOL.md), then
// restore `busy/working` in a finally block. agent-state.ts has NO heartbeat
// (it writes on state transitions only), so this tool must write
// waiting/asking itself before blocking; the shared flag below makes
// agent-state.ts emit waiting/asking too if any transition fires mid-question.
// This is reliable: the tool itself knows it is waiting for the user.

const QUESTION_FLAG = "__tmuxPanelQuestion";

// Where this extension really lives. jiti loads it as CJS and does NOT resolve
// symlinks, so when installed via a ~/.pi/agent/extensions symlink __dirname
// would point at the install dir — realpath fixes that so the relative
// colorize.sh path resolves into the repo.
const EXT_DIR: string = path.dirname(
	fs.realpathSync(
		typeof __filename !== "undefined"
			? __filename
			: fileURLToPath(import.meta.url),
	),
);

// Refresh window-label chips on state writes, same path resolution as
// agent-state.ts: TMUX_STATUS_COLORIZE overrides; empty string disables.
const COLORIZE =
	process.env.TMUX_STATUS_COLORIZE !== undefined
		? process.env.TMUX_STATUS_COLORIZE
		: path.join(EXT_DIR, "..", "..", "statusbar", "scripts", "colorize.sh");

type QuestionFlag = { active: true; since: number } | undefined;

function questionFlag(): QuestionFlag {
	return (globalThis as Record<string, unknown>)[QUESTION_FLAG] as QuestionFlag;
}

function setQuestionFlag(active: boolean): void {
	(globalThis as Record<string, unknown>)[QUESTION_FLAG] = active
		? { active: true as const, since: Date.now() }
		: undefined;
}

function tmuxSetState(payload: string | null): void {
	const pane = process.env.TMUX_PANE;
	if (!pane) return; // not inside tmux -> no-op
	const args = payload
		? ["set-option", "-p", "-t", pane, "@agent-state", payload]
		: ["set-option", "-u", "-p", "-t", pane, "@agent-state"];
	const p = spawn("tmux", args, { stdio: "ignore" });
	p.unref();
	p.on("error", () => {});
}

function writeAgentState(state: "waiting" | "busy", detail: string): void {
	const payload = JSON.stringify({
		tool: "pi",
		state,
		ts: Date.now() / 1000,
		since: Date.now() / 1000,
		detail,
	});
	tmuxSetState(payload);
	colorize(); // agent-state.ts only refreshes chips on ITS transitions — this
	// tool writes outside those transitions, so refresh here too or the chip
	// would stay on the pre-question colour until the next transition.
}

// Spawn colorize.sh (same contract as agent-state.ts). Failures are ignored
// (chips are optional; indicator.py re-refreshes them on status-bar redraws).
function colorize(): void {
	if (!COLORIZE) return;
	const pane = process.env.TMUX_PANE;
	if (!pane) return;
	const p = spawn("bash", [COLORIZE, pane], { stdio: "ignore" });
	p.unref();
	p.on("error", () => {});
}

// Options with labels and optional descriptions
const OptionSchema = Type.Object({
	label: Type.String({ description: "Display label for the option" }),
	description: Type.Optional(Type.String({ description: "Optional description shown below label" })),
});

const QuestionParams = Type.Object({
	question: Type.String({ description: "The question to ask the user" }),
	options: Type.Array(OptionSchema, { description: "Options for the user to choose from" }),
});

export default function question(pi: ExtensionAPI) {
	pi.registerTool({
		name: "question",
		label: "Question",
		description: "Ask the user a question and let them pick from options. Use when you need user input to proceed.",
		// ctx.ui.custom() replaces the single editor slot, so concurrent calls
		// clobber each other (only the last is answerable). Force this tool to
		// run sequentially within a batch so each question is shown and answered.
		// See https://github.com/earendil-works/pi/issues/3274
		executionMode: "sequential",
		parameters: QuestionParams,

		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			if (ctx.mode !== "tui") {
				return {
					content: [{ type: "text", text: "Error: UI not available (running in non-interactive mode)" }],
					details: {
						question: params.question,
						options: params.options.map((o) => o.label),
						answer: null,
					} as QuestionDetails,
				};
			}

			if (params.options.length === 0) {
				return {
					content: [{ type: "text", text: "Error: No options provided" }],
					details: { question: params.question, options: [], answer: null } as QuestionDetails,
				};
			}

			const allOptions: DisplayOption[] = [...params.options, { label: "Type something.", isOther: true }];

			setQuestionFlag(true);
			writeAgentState("waiting", "asking");
			try {
				const result = await ctx.ui.custom<{ answer: string; wasCustom: boolean; index?: number } | null>(
					(tui, theme, _kb, done) => {
					let optionIndex = 0;
					let editMode = false;
					let cachedLines: string[] | undefined;

					const editorTheme: EditorTheme = {
						borderColor: (s) => theme.fg("accent", s),
						selectList: {
							selectedPrefix: (t) => theme.fg("accent", t),
							selectedText: (t) => theme.fg("accent", t),
							description: (t) => theme.fg("muted", t),
							scrollInfo: (t) => theme.fg("dim", t),
							noMatch: (t) => theme.fg("warning", t),
						},
					};
					const editor = new Editor(tui, editorTheme);

					editor.onSubmit = (value) => {
						const trimmed = value.trim();
						if (trimmed) {
							done({ answer: trimmed, wasCustom: true });
						} else {
							editMode = false;
							editor.setText("");
							refresh();
						}
					};

					function refresh() {
						cachedLines = undefined;
						tui.requestRender();
					}

					function handleInput(data: string) {
						if (editMode) {
							if (matchesKey(data, Key.escape)) {
								editMode = false;
								editor.setText("");
								refresh();
								return;
							}
							editor.handleInput(data);
							refresh();
							return;
						}

						if (matchesKey(data, Key.up)) {
							optionIndex = Math.max(0, optionIndex - 1);
							refresh();
							return;
						}
						if (matchesKey(data, Key.down)) {
							optionIndex = Math.min(allOptions.length - 1, optionIndex + 1);
							refresh();
							return;
						}

						if (matchesKey(data, Key.enter)) {
							const selected = allOptions[optionIndex];
							if (selected.isOther) {
								editMode = true;
								refresh();
							} else {
								done({ answer: selected.label, wasCustom: false, index: optionIndex + 1 });
							}
							return;
						}

						if (matchesKey(data, Key.escape)) {
							done(null);
						}
					}

					function render(width: number): string[] {
						if (cachedLines) return cachedLines;

						const lines: string[] = [];
						const renderWidth = Math.max(1, width);

						function addWrapped(text: string) {
							lines.push(...wrapTextWithAnsi(text, renderWidth));
						}

						function addWrappedWithPrefix(prefix: string, text: string) {
							const prefixWidth = visibleWidth(prefix);
							if (prefixWidth >= renderWidth) {
								addWrapped(prefix + text);
								return;
							}
							const wrapped = wrapTextWithAnsi(text, renderWidth - prefixWidth);
							const continuationPrefix = " ".repeat(prefixWidth);
							for (let i = 0; i < wrapped.length; i++) {
								lines.push(`${i === 0 ? prefix : continuationPrefix}${wrapped[i]}`);
							}
						}

						lines.push(theme.fg("accent", "─".repeat(renderWidth)));
						addWrappedWithPrefix(" ", theme.fg("text", params.question));
						lines.push("");

						for (let i = 0; i < allOptions.length; i++) {
							const opt = allOptions[i];
							const selected = i === optionIndex;
							const isOther = opt.isOther === true;
							const prefix = selected ? theme.fg("accent", "> ") : "  ";
							const label = `${i + 1}. ${opt.label}${isOther && editMode ? " ✎" : ""}`;
							const color = selected || (isOther && editMode) ? "accent" : "text";

							addWrappedWithPrefix(prefix, theme.fg(color, label));

							// Show description if present
							if (opt.description) {
								addWrappedWithPrefix("     ", theme.fg("muted", opt.description));
							}
						}

						if (editMode) {
							lines.push("");
							addWrappedWithPrefix(" ", theme.fg("muted", "Your answer:"));
							for (const line of editor.render(Math.max(1, renderWidth - 2))) {
								lines.push(` ${line}`);
							}
						}

						lines.push("");
						if (editMode) {
							addWrappedWithPrefix(" ", theme.fg("dim", "Enter to submit • Esc to go back"));
						} else {
							addWrappedWithPrefix(" ", theme.fg("dim", "↑↓ navigate • Enter to select • Esc to cancel"));
						}
						lines.push(theme.fg("accent", "─".repeat(renderWidth)));

						cachedLines = lines;
						return lines;
					}

					return {
						render,
						invalidate: () => {
							cachedLines = undefined;
						},
						handleInput,
					};
				},
			);

			// Build simple options list for details
			const simpleOptions = params.options.map((o) => o.label);

			if (!result) {
				return {
					content: [{ type: "text", text: "User cancelled the selection" }],
					details: { question: params.question, options: simpleOptions, answer: null } as QuestionDetails,
				};
			}

			if (result.wasCustom) {
				return {
					content: [{ type: "text", text: `User wrote: ${result.answer}` }],
					details: {
						question: params.question,
						options: simpleOptions,
						answer: result.answer,
						wasCustom: true,
					} as QuestionDetails,
				};
			}
			return {
				content: [{ type: "text", text: `User selected: ${result.index}. ${result.answer}` }],
				details: {
					question: params.question,
					options: simpleOptions,
					answer: result.answer,
					wasCustom: false,
				} as QuestionDetails,
			};
			} finally {
				setQuestionFlag(false);
				writeAgentState("busy", "working");
			}
		},

		renderCall(args, theme, _context) {
			let text = theme.fg("toolTitle", theme.bold("question ")) + theme.fg("muted", args.question);
			const opts = Array.isArray(args.options) ? args.options : [];
			if (opts.length) {
				const labels = opts.map((o: OptionWithDesc) => o.label);
				const numbered = [...labels, "Type something."].map((o, i) => `${i + 1}. ${o}`);
				text += `\n${theme.fg("dim", `  Options: ${numbered.join(", ")}`)}`;
			}
			return new Text(text, 0, 0);
		},

		renderResult(result, _options, theme, _context) {
			const details = result.details as QuestionDetails | undefined;
			if (!details) {
				const text = result.content[0];
				return new Text(text?.type === "text" ? text.text : "", 0, 0);
			}

			if (details.answer === null) {
				return new Text(theme.fg("warning", "Cancelled"), 0, 0);
			}

			if (details.wasCustom) {
				return new Text(
					theme.fg("success", "✓ ") + theme.fg("muted", "(wrote) ") + theme.fg("accent", details.answer),
					0,
					0,
				);
			}
			const idx = details.options.indexOf(details.answer) + 1;
			const display = idx > 0 ? `${idx}. ${details.answer}` : details.answer;
			return new Text(theme.fg("success", "✓ ") + theme.fg("accent", display), 0, 0);
		},
	});
}
