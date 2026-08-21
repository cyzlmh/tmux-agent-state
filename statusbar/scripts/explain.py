#!/usr/bin/env python3
"""Explain why a pane shows its current state.

    explain.py [pane_id]        (default: $TMUX_PANE)

Prints the raw reader inputs (pane_dead, pane_current_command, @agent-state),
the PROTOCOL.md reader rule that fired, and the resulting classification —
the same agent_state.classify the status bar, chips and tmux-viz use.

Env: TMUX_STATUS_TMUX overrides the tmux command (e.g. "tmux -L socket" for
an isolated test server).
"""
from __future__ import annotations

import os
import subprocess
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
    r = subprocess.run(TMUX.split() + args, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _age(ts) -> str:
    if not isinstance(ts, (int, float)):
        return ""
    s = max(0, time.time() - ts)
    if s < 90:
        return f"{round(s)}s ago"
    if s < 5400:
        return f"{round(s / 60)}m ago"
    return f"{round(s / 3600)}h ago"


def main() -> None:
    pane = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TMUX_PANE", "")
    if not pane:
        sys.exit("usage: explain.py <pane_id>   (or run from inside the pane)")

    fmt = "#{pane_id}\t#{pane_dead}\t#{pane_current_command}\t#{window_name}\t#{@agent-state}"
    row = None
    for line in _tmux(["list-panes", "-a", "-F", fmt]).splitlines():
        f = line.split("\t")
        if len(f) >= 4 and f[0] == pane:
            row = (f[1], f[2], f[3], "\t".join(f[4:]) if len(f) > 4 else "")
            break
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


if __name__ == "__main__":
    main()
