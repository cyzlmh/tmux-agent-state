#!/usr/bin/env python3
"""Block until a pane reaches one of the given display states.

    wait.py [pane_id] STATE [STATE...] [--timeout SECONDS]

States are the shared display states (agent_state.display_state/classify):
running | needs-input | done | ready | stale | shell | untracked | dead.

The writer (adapters/agent-state.sh) signals the tmux wait-for channel
"agent-state-<pane>" on every transition; this script waits on that channel
and re-classifies after each wake, so spurious wakes are harmless. Prints the
reached state and exits 0; exits 1 on timeout (default: no timeout) or when
the pane does not exist.

Env: TMUX_STATUS_TMUX overrides the tmux command (e.g. "tmux -L socket").
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_state

TMUX = os.environ.get("TMUX_STATUS_TMUX", "tmux")


def main() -> None:
    args = [a for a in sys.argv[1:]]
    timeout = None
    if "--timeout" in args:
        i = args.index("--timeout")
        timeout = float(args[i + 1])
        del args[i : i + 2]
    if args and args[0].startswith("%"):
        pane = args.pop(0)
    else:
        pane = os.environ.get("TMUX_PANE", "")
    if not pane or not args:
        sys.exit(f"usage: {sys.argv[0]} [pane_id] STATE [STATE...] [--timeout SECONDS]")
    wanted = set(args)

    channel = f"agent-state-{pane}"
    deadline = None if timeout is None else time.time() + timeout
    while True:
        c = agent_state.classify_pane(TMUX, pane)
        if c is None:
            sys.exit(f"pane not found: {pane}")
        if c["state"] in wanted:
            print(c["state"])
            return
        remaining = None if deadline is None else deadline - time.time()
        if remaining is not None and remaining <= 0:
            sys.exit(f"timeout: still {c['state']} after {timeout:g}s")
        try:
            subprocess.run(
                TMUX.split() + ["wait-for", channel],
                capture_output=True,
                timeout=remaining,  # None -> wait forever
            )
        except subprocess.TimeoutExpired:
            pass  # deadline reached; the loop re-checks and exits


if __name__ == "__main__":
    main()
