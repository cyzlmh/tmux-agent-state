#!/usr/bin/env python3
"""Shared reader logic for the @agent-state protocol (PROTOCOL.md).

Single source of truth for classification. Used by:
  - statusbar/scripts/indicator.py  (status segment)
  - statusbar/scripts/colorize.sh   (window chips, via the CLI below)
  - statusbar/scripts/explain.py    (per-pane debugging)
  - tmux-viz/tmux_viz.py            (web visualiser)

Reader rules (precedence, explicit signals only — no guessing):
    1. pane_dead == 1                                -> dead
    2. @agent-state present, foreground not a shell  -> adapter's state, live
       regardless of age (no heartbeat; ts is display-only, never liveness)
    3. @agent-state present, foreground is a shell   -> stale (adapter gone)
    4. no @agent-state, foreground is a shell        -> shell (idle)
    5. otherwise                                     -> untracked

Display states (wire -> display):
    busy               -> running
    waiting + asking   -> needs-input   the only attention state
    waiting + done     -> done
    waiting (other)    -> ready
    plus stale / shell / untracked / dead

CLI for shell consumers (colorize.sh):

    AGENT_STATE='<json>' python3 agent_state.py wire-state

prints the display state for a wire payload (running | needs-input | done |
ready), nothing for a missing/unusable payload.
"""
from __future__ import annotations

import json
import os
import sys

# pane_current_command values that mean "no agent process is running"
SHELLS = {"zsh", "bash", "fish", "sh", "dash", "tcsh", "ksh", "nu"}


def parse_payload(raw: str) -> dict | None:
    """Parse an @agent-state payload; None when missing or unusable.

    A payload without a valid state or ts is not trusted: adapters write both
    on every write, so a partial value means something else wrote the option.
    """
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or d.get("state") not in ("waiting", "busy"):
        return None
    if not isinstance(d.get("ts"), (int, float)):
        return None
    return d


def display_state(state: str, detail: str) -> str:
    """Wire state + detail -> display state."""
    if state == "busy":
        return "running"
    if detail == "asking":
        return "needs-input"
    if detail == "done":
        return "done"
    return "ready"


def classify(dead: str, cmd: str, raw: str) -> dict:
    """Apply the reader rules -> {tool, state, source, ts, rule}.

    Liveness is a tmux fact (pane_current_command), never ts: a live adapter's
    state is trusted no matter how old, a shell foreground means the adapter
    process is gone (stale). `rule` records which reader rule fired, for
    explain.py and tests.
    """
    if dead == "1":
        return {"tool": "?", "state": "dead", "source": "-", "ts": None, "rule": 1}
    adapter = parse_payload(raw)
    if adapter:
        tool = str(adapter.get("tool") or "?")
        if cmd.lstrip("-") in SHELLS:
            return {"tool": tool, "state": "stale", "source": "adapter",
                    "ts": adapter.get("ts"), "rule": 3}
        return {"tool": tool,
                "state": display_state(adapter["state"], str(adapter.get("detail") or "")),
                "source": "adapter", "ts": adapter.get("ts"), "rule": 2}
    if cmd.lstrip("-") in SHELLS:
        return {"tool": "shell", "state": "shell", "source": "tmux", "ts": None, "rule": 4}
    return {"tool": "?", "state": "untracked", "source": "tmux", "ts": None, "rule": 5}


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "wire-state":
        d = parse_payload(os.environ.get("AGENT_STATE", ""))
        if d:
            print(display_state(d["state"], str(d.get("detail") or "")))
        return
    sys.exit(f"usage: {sys.argv[0]} wire-state   (payload in $AGENT_STATE)")


if __name__ == "__main__":
    main()
