#!/usr/bin/env python3
"""Explain why a pane shows its current state.

    explain.py [pane_id] [--history N]

Prints the raw reader inputs (pane_dead, pane_current_command, @agent-state),
the PROTOCOL.md reader rule that fired, and the resulting classification —
the same agent_state.classify the status bar, chips and tmux-viz use.

--history N also prints the last N transitions of the pane from the JSONL
log ($TMUX_AGENT_STATE_LOG, default ${TMPDIR:-/tmp}/tmux-agent-state.log).

Env: TMUX_STATUS_TMUX overrides the tmux command (e.g. "tmux -L socket" for
an isolated test server).
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_state

TMUX = os.environ.get("TMUX_STATUS_TMUX", "tmux")

RULES = {
    1: "pane_dead == 1 -> dead (always wins)",
    2: "@agent-state present, foreground not a shell -> adapter's state, live "
       "regardless of age (no heartbeat; ts never decides liveness)",
    3: "@agent-state present, foreground is a shell -> stale (adapter process gone)",
    4: "no @agent-state, foreground is a shell -> shell (idle; tmux fact, exact)",
    5: "otherwise -> untracked (no live adapter; reported honestly, never inferred)",
}


def _tmux(args: list[str]) -> str:
    return agent_state.tmux_run(TMUX, args)


def _age(ts) -> str:
    if not isinstance(ts, (int, float)):
        return ""
    s = max(0, time.time() - ts)
    if s < 90:
        return f"{round(s)}s ago"
    if s < 5400:
        return f"{round(s / 60)}m ago"
    return f"{round(s / 3600)}h ago"


def _log_path() -> str:
    return os.environ.get(
        "TMUX_AGENT_STATE_LOG",
        os.path.join(os.environ.get("TMPDIR", "/tmp"), "tmux-agent-state.log"),
    )


def _history(pane: str, limit: int) -> None:
    try:
        lines = open(_log_path(), encoding="utf-8").read().splitlines()
    except OSError:
        print(f"\nhistory: no log at {_log_path()}")
        return
    rows = []
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("pane") == pane:
            rows.append(d)
    print(f"\nhistory (last {limit}, {_log_path()}):")
    if not rows:
        print("  (no transitions recorded for this pane)")
        return
    for d in rows[-limit:]:
        tool = d.get("tool") or "-"
        detail = d.get("detail") or ""
        tail = f"/{detail}" if detail else ""
        print(f"  {_age(d.get('ts')):>9}  {tool} {d.get('state', '?')}{tail}")


def main() -> None:
    argv = sys.argv[1:]
    history = None
    if "--history" in argv:
        i = argv.index("--history")
        history = int(argv[i + 1]) if i + 1 < len(argv) else 10
        del argv[i : i + 2]
    pane = argv[0] if argv else os.environ.get("TMUX_PANE", "")
    if not pane:
        sys.exit("usage: explain.py <pane_id> [--history N]   (or run from inside the pane)")

    row = agent_state.read_pane(TMUX, pane)
    if row is None:
        sys.exit(f"pane not found: {pane}")
    dead, cmd, window, raw = row

    c = agent_state.classify(dead, cmd, raw)
    print(f"pane                  {pane}  (window: {window})" if window else f"pane  {pane}")
    print(f"pane_dead             {dead}")
    shell = cmd.lstrip("-") in agent_state.SHELLS
    print(f"pane_current_command  {cmd}" + ("  (shell)" if shell else ""))
    d = agent_state.parse_payload(raw)
    if d:
        print(f"@agent-state          {raw}")
        print(f"  ts                  {_age(d.get('ts'))} (display only, never liveness)")
    elif raw:
        print(f"@agent-state          (unusable payload: {raw})")
    else:
        print("@agent-state          (empty)")
    print()
    print(f"rule {c['rule']}: {RULES[c['rule']]}")
    print(f"-> state={c['state']}  tool={c['tool']}  source={c['source']}")
    if history is not None:
        _history(pane, history)


if __name__ == "__main__":
    main()
