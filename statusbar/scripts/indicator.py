#!/usr/bin/env python3
"""tmux-agent-state status-bar segment: session-level agent statistics.

The status bar shows the *same content in every window* (it is scoped to the
session), so instead of per-pane icons it reports aggregate counts — how many
agents in the current session are in each state:

    ?3 ✓2 !1 ▶1       3 asking (waiting for your input) / 2 done / 1 stale / 1 running

Counts with zero agents are omitted; an empty session shows nothing. Per-pane
detail belongs to the window label (colour chips, see colorize.sh), not here.

Add to your status bar (after running statusbar/statusbar.tmux once):

    set -g status-right '#{agent_status} | %H:%M'

Config (tmux global user options, e.g. `set -g @agent-status-scope window`):
  @agent-status-enabled           on/off                     (default on)
  @agent-status-scope             session|window             (default session)
  @agent-status-color-needs-input needs-input colour         (default colour214)
  @agent-status-color-done        done colour                (default colour34)
  @agent-status-color-stale       stale colour               (default colour161)
  @agent-status-color-running     running colour             (default colour39)

Chips refresh: this script doubles as the periodic trigger for colorize.sh
(window-label colour chips). Adapters only invoke colorize.sh on state
*transitions*, but staleness and adapter shutdown (option cleared) are not
transitions, so without a periodic trigger a dead agent's chip would keep its
last colour forever. tmux re-runs #() commands every status-interval, which
makes this script a natural refresher; the refresh is throttled (see below).

Config (env, mainly for tests):
  TMUX_STATUS_TMUX            tmux command override (e.g. "tmux -L socket")
  TMUX_STATUS_COLORIZE        colorize.sh path override; empty disables refresh
  TMUX_STATUS_CHIPS_REFRESH   min seconds between chips refreshes (default 15)

State mapping (@agent-state wire -> display, reader rules from PROTOCOL.md):
  busy                          -> running
  waiting + detail=asking       -> needs-input   (agent is asking you)
  waiting + detail=done         -> done          (agent finished a turn)
  waiting + detail=ready        -> (not counted) (idle)
  @agent-state present but the pane foreground is a shell (adapter process
  gone — no heartbeat in this protocol) -> stale
"""
from __future__ import annotations

import json
import os
import subprocess
import time

TMUX = os.environ.get("TMUX_STATUS_TMUX", "tmux")  # test/override hook
CHIPS_REFRESH = float(os.environ.get("TMUX_STATUS_CHIPS_REFRESH", "15"))
COLORIZE = os.environ.get(
    "TMUX_STATUS_COLORIZE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "colorize.sh"),
)

SYMBOLS = {"needs-input": "?", "done": "✓", "stale": "!", "running": "▶"}
ORDER = ["needs-input", "done", "stale", "running"]
DEFAULT_COLORS = {
    "needs-input": "colour214",
    "done": "colour34",
    "stale": "colour161",
    "running": "colour39",
}

# pane_current_command values that mean "no agent process is running"
SHELLS = {"zsh", "bash", "fish", "sh", "dash", "tcsh", "ksh", "nu"}


def _tmux(args: list[str]) -> str:
    r = subprocess.run(TMUX.split() + args, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def get_opt(name: str, default: str) -> str:
    v = _tmux(["show-option", "-gqv", name]).strip()
    return v if v else default


def is_on(value: str) -> bool:
    return value.strip().lower() in ("on", "true", "yes", "1")


def parse_state(raw: str):
    """Parse an @agent-state payload -> (tool, state, detail, ts) or None."""
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    st = d.get("state")
    if st not in ("busy", "waiting"):
        return None
    ts = d.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    return (str(d.get("tool") or "?"), st, str(d.get("detail") or ""), ts)


def display_state(state: str, detail: str):
    """Map wire state -> display state (None = not counted)."""
    if state == "busy":
        return "running"
    if detail == "asking":
        return "needs-input"
    if detail == "done":
        return "done"
    return None  # waiting + ready: idle


def classify(state_raw: str, cmd: str):
    """@agent-state payload + pane current command -> display state or None.

    stale is decided by process presence, not ts: with no heartbeat, a pane
    whose foreground is a shell no longer runs its adapter.
    """
    p = parse_state(state_raw)
    if not p:
        return None
    if cmd.lstrip("-") in SHELLS:
        return "stale"
    return display_state(p[1], p[2])


def count_stats(entries) -> dict:
    """entries: list of (state_raw, pane_current_command)."""
    counts = {k: 0 for k in ORDER}
    for raw, cmd in entries:
        ds = classify(raw, cmd)
        if ds:
            counts[ds] += 1
    return counts


def render_stats(counts: dict, colors: dict) -> str:
    parts = []
    for k in ORDER:
        n = counts.get(k, 0)
        if not n:
            continue
        style = f"fg={colors.get(k, 'default')}"
        if k == "needs-input":
            style += ",bold"
        parts.append(f"#[{style}]{SYMBOLS[k]}{n}#[default]")
    return " ".join(parts)


def _current_session() -> str:
    """Session name of the currently focused pane/session. The status bar
    runs #() commands with the tmux global environment (no client context), so
    fall back to $TMUX_PANE when display-message needs a target."""
    s = os.environ.get("TMUX_STATUS_SESSION") or _tmux(
        ["display-message", "-p", "#{session_name}"]
    ).strip()
    if not s and os.environ.get("TMUX_PANE"):
        s = _tmux(["display-message", "-p", "-t", os.environ["TMUX_PANE"], "#{session_name}"]).strip()
    return s


def _current_window() -> str:
    w = os.environ.get("TMUX_STATUS_WINDOW") or _tmux(
        ["display-message", "-p", "#{window_id}"]
    ).strip()
    if not w and os.environ.get("TMUX_PANE"):
        w = _tmux(["display-message", "-p", "-t", os.environ["TMUX_PANE"], "#{window_id}"]).strip()
    return w


def refresh_chips(window: str, now: float) -> None:
    """Re-run colorize.sh for the current window, throttled to CHIPS_REFRESH.

    Covers the cases adapters cannot: a pane going stale (adapter process
    gone — not a transition) and an adapter clearing its option on shutdown
    are not state transitions, so nothing else would update the chips. The
    throttle timestamp lives in the
    window option @agent-status-chips-ts. The spawned colorize.sh inherits
    $TMUX (tmux sets it for #() jobs) or $TMUX_STATUS_TMUX, so it talks to the
    right server. Fire-and-forget: failures just mean chips update next time.
    """
    if not COLORIZE or not window:
        return
    last = _tmux(["show-option", "-wqv", "-t", window, "@agent-status-chips-ts"]).strip()
    try:
        if now - float(last) < CHIPS_REFRESH:
            return
    except ValueError:
        pass
    panes = _tmux(["list-panes", "-t", window, "-F", "#{pane_id}"]).split()
    if not panes:
        return
    _tmux(["set-option", "-wq", "-t", window, "@agent-status-chips-ts", str(int(now))])
    try:
        subprocess.Popen(
            ["bash", COLORIZE, panes[0]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def main() -> None:
    if not is_on(get_opt("@agent-status-enabled", "on")):
        print("")
        return

    session = _current_session()
    if not session:
        print("")
        return

    scope = get_opt("@agent-status-scope", "session")
    window = _current_window()
    fmt = "#{session_name}\t#{pane_id}\t#{pane_dead}\t#{pane_current_command}\t#{@agent-state}"
    if scope == "window":
        if not window:
            print("")
            return
        out = _tmux(["list-panes", "-t", window, "-F", fmt])
    else:
        # -t <session> only lists the session's *current* window, so list all
        # and filter by session name ourselves
        out = _tmux(["list-panes", "-a", "-F", fmt])

    colors = {k: get_opt(f"@agent-status-color-{k}", v) for k, v in DEFAULT_COLORS.items()}

    entries = []
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) < 5:
            continue
        sess, pane_id, dead, cmd, raw = f[0], f[1], f[2], f[3], "\t".join(f[4:])
        if sess != session or dead == "1":
            continue
        entries.append((raw, cmd))

    refresh_chips(window, time.time())
    print(render_stats(count_stats(entries), colors))


if __name__ == "__main__":
    main()
