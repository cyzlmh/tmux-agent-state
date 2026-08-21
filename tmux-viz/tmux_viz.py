#!/usr/bin/env python3
"""tmux visualiser — the web consumer of @agent-state (see PROTOCOL.md).

    python3 tmux_viz.py                 # 127.0.0.1:20010 (localhost only)

Pages
    GET /                       overview: one card per window
    GET /w/<session>/<index>    detail:   that window's panes, full scrollable content

API
    GET /api/overview                       summary: tool + display state + 2-line peek
    GET /api/window/<session>/<index>       one window with full pane content
    GET /api/state?lines=80                 everything (legacy)
    GET /api/pane/<id>?lines=80             one pane content (legacy)

Reader rules (per pane, PROTOCOL.md — explicit signals only, no guessing):
    1. pane_dead == 1                                -> dead
    2. @agent-state present, foreground not a shell  -> adapter's state, live
       regardless of age (no heartbeat; ts is display-only, never liveness)
    3. @agent-state present, foreground is a shell   -> stale (adapter gone)
    4. no @agent-state, foreground is a shell        -> shell (idle)
    5. otherwise                                     -> untracked

Classification lives in the shared module statusbar/scripts/agent_state.py
(also used by the status segment, colorize.sh and explain.py).

Display states (wire -> display):
    busy               -> running     (▶)
    waiting + asking   -> needs-input (?)   the only attention state
    waiting + done     -> done        (✓)
    waiting (other)    -> ready       (·)
    plus stale (!) / shell / untracked / dead (✕)

`source` marks where the state came from: "adapter" | "tmux".
@agent-io (last interaction) is shown only while the adapter is live (rule 2);
stale panes fall back to the raw terminal capture.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "statusbar", "scripts"),
)
import agent_state

TMUX = "tmux"
HOST = socket.gethostname()

# pause between literal text and the trailing Enter so TUIs don't read the
# pair as a paste (see do_POST)
ENTER_DELAY = 0.12  # seconds

PANE_FMT = "\t".join([
    "#{session_name}", "#{window_index}", "#{window_name}",
    "#{pane_index}", "#{pane_id}", "#{pane_width}", "#{pane_height}",
    "#{pane_title}", "#{pane_dead}", "#{pane_active}",
    "#{pane_current_command}", "#{@agent-state}",
])

# detail page also reads the last-interaction I/O option
PANE_FMT_IO = PANE_FMT + "\t" + "#{@agent-io}"

# ---------- state classification (shared module: statusbar/scripts/agent_state.py) ----------

# re-exported for callers/tests that import them from here
display_state = agent_state.display_state


def _classify(p: dict) -> dict:
    """Reader rules -> {tool, state, source, ts, rule} (see agent_state.py)."""
    return agent_state.classify(p["dead"], p["cmd"], p["agent_state"])

# display-state priority for window/session aggregation: the first state
# present wins. needs-input is the only attention state, so it always leads.
PRIORITY = ["needs-input", "stale", "running", "done",
            "ready", "shell", "untracked", "dead"]

_ANSI_RE = re.compile(
    r'\x1b\[[0-9;?]*[a-zA-Z]'
    r'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'
    r'|\x1b[()][AB012]'
    r'|\x1b[=>]'
    r'|\r'
)


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub('', s)


def _tmux(args: list[str]) -> str:
    r = subprocess.run([TMUX, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def capture(pane_id: str, lines: int, escapes: bool) -> str:
    args = ["capture-pane", "-p", "-t", pane_id, "-S", f"-{lines}"]
    if escapes:
        args.append("-e")
    try:
        return _tmux(args)
    except RuntimeError:
        return ""


def _parse(line: str) -> dict | None:
    f = line.split("\t")
    if len(f) < 11:
        return None
    # 12th field (@agent-state) may be missing on older tmux / empty panes
    state_raw = f[11] if len(f) > 11 else ""
    return {
        "session": f[0], "windex": f[1], "wname": f[2],
        "pindex": f[3], "pid": f[4], "pw": f[5], "ph": f[6],
        "title": f[7], "dead": f[8], "active": f[9], "cmd": f[10],
        "agent_state": state_raw,
        "io_raw": f[12] if len(f) > 12 else "",
    }


# ---------- state classification: shared module (imported above) ----------


def _io(raw: str) -> dict | None:
    """Parse the @agent-io payload ({input, output, ts}) if present."""
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or "input" not in d:
        return None
    return {"input": str(d.get("input", "")), "output": str(d.get("output", "")),
            "ts": d.get("ts")}


def _agg(panes: list[dict]) -> str:
    if not panes:
        return "empty"
    states = {p["state"] for p in panes}
    for s in PRIORITY:
        if s in states:
            return s
    return "empty"


def _tail(content: str, n: int = 2, maxlen: int = 160) -> str:
    lines = [ln.rstrip() for ln in strip_ansi(content).splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    txt = "\n".join(lines[-n:]) if lines else ""
    return txt if len(txt) <= maxlen else txt[:maxlen - 1] + "…"


def _pane_view(p: dict, c: dict, content: str, with_content: bool,
               io: dict | None = None) -> dict:
    return {
        "id": p["pid"],
        "index": p["pindex"],
        "active": p["active"] == "1",
        "size": f"{p['pw']}x{p['ph']}",
        "title": p["title"],
        "tool": c["tool"],
        "state": c["state"],
        "source": c["source"],
        "ts": c["ts"],
        "io": io,
        "tail": _tail(content) if not with_content else "",
        "content": content if with_content else "",
    }


def collect_overview() -> dict:
    try:
        out = _tmux(["list-panes", "-a", "-F", PANE_FMT])
    except RuntimeError:
        out = ""
    windows: dict[str, dict] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        p = _parse(line)
        if not p:
            continue
        content = "" if p["dead"] == "1" else capture(p["pid"], 14, escapes=False)
        c = _classify(p)
        pv = _pane_view(p, c, content, False)
        key = f"{p['session']}\t{p['windex']}"
        w = windows.setdefault(key, {
            "session": p["session"], "index": p["windex"],
            "name": p["wname"], "panes": []})
        w["panes"].append(pv)

    win_list = []
    for key in sorted(windows, key=lambda k: (k.split("\t")[0], int(k.split("\t")[1]))):
        w = windows[key]
        w["state"] = _agg(w["panes"])
        win_list.append(w)
    return {
        "windows": win_list,
        "totals": {
            "sessions": len({w["session"] for w in win_list}),
            "windows": len(win_list),
            "panes": sum(len(w["panes"]) for w in win_list),
        },
    }


def collect_window(session: str, index: str, lines: int = 250) -> dict:
    out = _tmux(["list-panes", "-t", f"{session}:{index}", "-F", PANE_FMT_IO])
    panes = []
    name = ""
    for line in out.splitlines():
        if not line.strip():
            continue
        p = _parse(line)
        if not p:
            continue
        name = p["wname"]
        content = capture(p["pid"], lines, escapes=True)
        c = _classify(p)
        # last-interaction I/O is trusted only while the adapter is live;
        # stale panes fall back to the raw capture (PROTOCOL.md)
        io = _io(p["io_raw"]) if c["source"] == "adapter" and c["state"] != "stale" else None
        panes.append(_pane_view(p, c, content, True, io=io))
    return {"session": session, "index": index, "name": name,
            "panes": panes, "state": _agg(panes)}


# ---------- pages ----------

# Design language (see README "Design language"): monochrome chrome, state
# colours only. One palette shared with the status bar — tmux 256-colour
# values and their hex equivalents:
#   needs-input colour180 #d7af87 (the only attention colour, bold)
#   done        colour108 #87af87
#   running     colour68  #5f87d7
#   stale       colour167 #d75f5f
#   chrome      bg colour234 #1c1c1c / panel #262626 / line #3a3a3a /
#               fg colour250 #bcbcbc / muted #808080
_STYLE = """
:root{
  --bg:#1c1c1c;--panel:#262626;--line:#3a3a3a;--sunken:#161616;
  --fg:#bcbcbc;--muted:#808080;
  --needs:#d7af87;--done:#87af87;--run:#5f87d7;--stale:#d75f5f;
}
body{font:15px/1.5 -apple-system,system-ui,sans-serif;margin:0;background:var(--bg);color:var(--fg);-webkit-text-size-adjust:100%}
header{padding:9px 14px;background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:2;font-size:13px}
a{color:var(--run);text-decoration:none}
.muted{color:var(--muted)}
.empty{color:var(--muted);padding:20px}
.session{max-width:1100px;margin:0 auto}
.session h2{font:600 13px -apple-system,system-ui,sans-serif;margin:0;padding:12px 14px 4px;display:flex;align-items:center;gap:6px}
.session h2 .muted{font-weight:400}
.grid{display:flex;flex-direction:column;gap:8px;padding:8px 14px 14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:6px;overflow:hidden;display:block;color:inherit;text-decoration:none}
.card.needs-input{border-left:3px solid var(--needs)}
.card .hd{display:flex;justify-content:space-between;align-items:baseline;gap:8px;padding:8px 10px;border-bottom:1px solid var(--line);font-size:13px}
.card .hd .t{font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge{font:12px ui-monospace,Menlo,monospace;white-space:nowrap}
.prow{display:flex;gap:8px;align-items:baseline;padding:5px 10px;border-top:1px solid #2b2b2b;font:12px ui-monospace,Menlo,monospace;min-width:0}
.tool{font-weight:600;min-width:52px}
.sz,.age{color:var(--muted)}
.peek{padding:2px 10px 8px;font:12px/1.35 ui-monospace,Menlo,monospace;color:var(--muted);white-space:pre;overflow:hidden;max-height:2.7em}
@media (max-width:600px){
  .grid{padding:6px 8px 10px;gap:6px}
  .session h2{padding:10px 10px 2px}
  .prow .sz{display:none}
}
.sym{font-family:ui-monospace,Menlo,monospace;display:inline-block;min-width:1.1em;text-align:center}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;vertical-align:middle}
.needs-input{color:var(--needs);font-weight:600}
.done{color:var(--done)}
.running{color:var(--run)}
.stale{color:var(--stale)}
.dead,.ready,.shell,.untracked{color:var(--muted);font-weight:400}
.dot.needs-input{background:var(--needs)}
.dot.done{background:var(--done)}
.dot.running{background:var(--run)}
.dot.stale{background:var(--stale)}
.dot.dead,.dot.ready,.dot.shell,.dot.untracked,.dot.empty{background:var(--muted)}
.detail{display:flex;flex-direction:column;height:100vh;height:100dvh;overflow:hidden}
.detail .tabs{display:flex;gap:6px;padding:6px 10px;overflow-x:auto;background:var(--panel);border-bottom:1px solid var(--line);flex:0 0 auto;-webkit-overflow-scrolling:touch}
.detail .tab{font:12px ui-monospace,Menlo,monospace;padding:5px 10px;border-radius:6px;background:transparent;border:1px solid var(--line);color:var(--fg);cursor:pointer;display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
.detail .tab.sel{border-color:var(--fg)}
.detail .toolbar{display:flex;align-items:center;gap:8px;padding:6px 10px;background:var(--panel);border-bottom:1px solid var(--line);flex:0 0 auto;font:12px ui-monospace,Menlo,monospace;color:var(--muted)}
.detail .toolbar button{font:12px ui-monospace,Menlo,monospace;padding:3px 10px;border-radius:6px;background:transparent;border:1px solid var(--line);color:var(--fg);cursor:pointer}
.detail .toolbar button.on{border-color:var(--fg)}
.detail .toolbar .src{margin-left:auto}
.detail .content{flex:1 1 auto;min-height:0;overflow:auto;background:var(--sunken);-webkit-overflow-scrolling:touch}
.detail .raw{margin:0;padding:10px 12px;white-space:pre;font:13px/1.45 ui-monospace,Menlo,monospace;color:var(--fg)}
.detail .io .in{padding:8px 12px;background:var(--panel);border-bottom:1px solid var(--line);font:13px/1.4 ui-monospace,Menlo,monospace;color:var(--fg);white-space:pre-wrap;word-break:break-word;position:sticky;top:0;z-index:1}
.detail .io .in .lbl{color:var(--muted);margin-right:6px}
.detail .io .out{margin:0;padding:10px 12px;white-space:pre-wrap;word-break:break-word;font:13px/1.45 ui-monospace,Menlo,monospace;color:var(--fg)}
.detail .io .pending{color:var(--muted);font-style:italic}
.inputbar{display:flex;gap:6px;padding:8px 10px;background:var(--panel);border-top:1px solid var(--line);flex:0 0 auto;align-items:flex-end}
.inputbar textarea{flex:1 1 auto;min-width:0;font:14px/1.4 ui-monospace,Menlo,monospace;padding:7px 10px;border-radius:6px;border:1px solid var(--line);background:var(--sunken);color:var(--fg);resize:none}
.inputbar button{font:13px ui-monospace,Menlo,monospace;padding:7px 12px;border-radius:6px;border:1px solid var(--line);background:transparent;color:var(--fg);cursor:pointer;flex:0 0 auto}
.inputbar button:active{border-color:var(--fg)}
"""

OVERVIEW_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>""" + HOST + """ · tmux</title>
<style>""" + _STYLE + """</style></head><body>
<header><b>tmux</b> <span class="muted">@ """ + HOST + """</span> · <span id="meta">loading…</span> · <a href="#" id="pause">pause</a></header>
<div id="root"></div>
<script>
const esc = s => (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const PRIORITY = ['needs-input','stale','running','done','ready','shell','untracked','dead'];
const SYM = {'needs-input':'?','done':'✓','running':'▶','stale':'!','dead':'✕'};
const sym = s => SYM[s] || '·';
const agg = panes => {const set=new Set(panes.map(p=>p.state));for(const s of PRIORITY)if(set.has(s))return s;return 'empty';};
const age = ts => {if(!ts)return'';const s=Math.max(0,Date.now()/1000-ts);
  if(s<90)return Math.round(s)+'s';if(s<5400)return Math.round(s/60)+'m';return Math.round(s/3600)+'h';};
let paused = false;
document.getElementById('pause').onclick = e=>{e.preventDefault();paused=!paused;
  e.target.textContent=paused?'resume':'pause';};
async function refresh(){
  if(paused) return;
  try{
    const d = await (await fetch('/api/overview')).json();
    const t = d.totals;
    const tally = {'needs-input':0,done:0,stale:0,running:0};
    for(const w of d.windows) for(const p of w.panes) if(p.state in tally) tally[p.state]++;
    const seg = k => tally[k] ? `<span class="${k}">${sym(k)}${tally[k]}</span>` : '';
    document.getElementById('meta').innerHTML =
      [`${t.sessions}s · ${t.windows}w · ${t.panes}p`, seg('needs-input'), seg('done'),
       seg('stale'), seg('running'), new Date().toLocaleTimeString()].filter(Boolean).join(' · ');
    const root = document.getElementById('root'); root.innerHTML='';
    if(!d.windows.length){root.innerHTML='<div class="empty">no tmux sessions</div>';return;}
    const bySess = {};
    for(const w of d.windows){ (bySess[w.session] ??= []).push(w); }
    const cardEl = (w) => {
      const a=document.createElement('a'); a.className='card '+w.state;
      a.href='/w/'+encodeURIComponent(w.session)+'/'+w.index;
      a.innerHTML=
        `<div class="hd"><span class="t"><span class="dot ${w.state}"></span> [${w.index}] ${esc(w.name)}</span>`
        +`<span class="badge ${w.state}">${sym(w.state)} ${w.state}</span></div>`;
      for(const p of w.panes){
        const row=document.createElement('div'); row.className='prow';
        row.innerHTML=`<span class="sym ${p.state}">${sym(p.state)}</span>`
          +`<span class="tool">${esc(p.tool)}</span>`
          +`<span class="${p.state}">${p.state}</span>`
          +`<span class="sz">${esc(p.size)}</span>`
          +(p.ts?`<span class="age">${age(p.ts)}</span>`:'')
          +(p.active?'<span class="needs-input">●</span>':'');
        a.appendChild(row);
        if(p.tail){const pk=document.createElement('div');pk.className='peek';pk.textContent=p.tail;a.appendChild(pk);}
      }
      return a;
    };
    // attention first: needs-input sessions and windows float to the top,
    // stable order otherwise (session name, window index)
    const sAttn = s => bySess[s].some(w=>w.state==='needs-input') ? 0 : 1;
    for(const sname of Object.keys(bySess).sort((a,b)=>sAttn(a)-sAttn(b)||a.localeCompare(b))){
      const wins=bySess[sname].slice().sort(
        (a,b)=>((a.state==='needs-input'?0:1)-(b.state==='needs-input'?0:1))||(a.index-b.index));
      const sState = agg(wins.flatMap(w=>w.panes));
      const sec=document.createElement('div'); sec.className='session';
      sec.innerHTML=`<h2><span class="dot ${sState}"></span> ${esc(sname)} <span class="muted">${wins.length}w</span></h2>`;
      const grid=document.createElement('div'); grid.className='grid';
      for(const w of wins) grid.appendChild(cardEl(w));
      sec.appendChild(grid); root.appendChild(sec);
    }
  }catch(e){document.getElementById('meta').textContent='error: '+e;}
}
refresh(); setInterval(refresh, 2000);
</script></body></html>"""


def detail_html(session: str, index: str, name: str) -> str:
    key = f"{unquote(session)}/{index}"
    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>""" + HOST + """ · """ + f"{unquote(session)}:{index}" + """</title>
<style>""" + _STYLE + """</style>
<script src="https://cdn.jsdelivr.net/npm/ansi_up@5.2.1/ansi_up.min.js"></script>
</head><body class="detail">
<header><a href="/">← overview</a> · <span class="muted">""" + HOST + """</span> · <b>""" + f"{unquote(session)}:{index} {esc(name)}" + """</b>
 · <span id="meta">loading…</span> · <a href="#" id="pause">pause</a></header>
<nav class="tabs" id="tabs"></nav>
<div class="toolbar">
  <span>view:</span>
  <button id="b-last" class="on">last I/O</button>
  <button id="b-raw">raw</button>
  <span id="src" class="src"></span>
</div>
<div class="content" id="content"></div>
<div class="inputbar"><textarea id="intext" rows="2" placeholder="send to pane… (⌘⏎ send, ⏎ newline)"></textarea><button id="send">send ⌘⏎</button><button class="sk" data-key="Escape">Esc</button><button class="sk" data-key="C-c">⌃C</button></div>
<script>
const au = new AnsiUp();
const esc = s => (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const SYM = {'needs-input':'?','done':'✓','running':'▶','stale':'!','dead':'✕'};
const sym = s => SYM[s] || '·';
const KEY = """ + json.dumps(key) + """;
let panes=[], sel=null, mode='last', paused=false;
const byId = id => panes.find(p=>p.id===id) || panes[0] || null;
document.getElementById('pause').onclick = e=>{e.preventDefault();paused=!paused;
  e.target.textContent=paused?'resume':'pause';};
function viewFor(p){ return (mode==='last' && p.io) ? 'io' : 'raw'; }
function renderTabs(){
  const t=document.getElementById('tabs'); t.innerHTML='';
  for(const p of panes){
    const b=document.createElement('button'); b.className='tab'+(p.id===sel?' sel':'');
    b.innerHTML=`<span class="sym ${p.state}">${sym(p.state)}</span>${esc(p.tool)} <span class="muted">${esc(p.id)}</span>`+(p.active?' ●':'');
    b.onclick=()=>{sel=p.id; renderTabs(); renderContent(true);};
    t.appendChild(b);
  }
}
function renderContent(toBottom){
  const p=byId(sel); if(!p) return;
  const c=document.getElementById('content');
  // stick to bottom when explicitly asked (pane/mode switch) or when the
  // user is already near the bottom; scrolling up pins the view in place
  const stick = toBottom || (c.scrollHeight - c.scrollTop - c.clientHeight < 40);
  document.getElementById('src').textContent =
    p.io ? 'adapter · live' : (p.state==='stale' ? 'adapter gone · raw' : 'raw');
  if(viewFor(p)==='io'){
    const out = p.io.output ? esc(p.io.output) : '<span class="pending">(waiting for output…)</span>';
    c.innerHTML=`<div class="io"><div class="in"><span class="lbl">you ▶</span>${esc(p.io.input||'')}</div><pre class="out">${out}</pre></div>`;
  } else {
    c.innerHTML=`<pre class="raw">${au.ansi_to_html(p.content||'')}</pre>`;
  }
  if(stick) c.scrollTop=c.scrollHeight;
}
function render(toBottom){renderTabs();renderContent(toBottom);}
function setMode(m){
  mode=m;
  document.getElementById('b-last').classList.toggle('on', m==='last');
  document.getElementById('b-raw').classList.toggle('on', m==='raw');
  renderContent(true);
}
document.getElementById('b-last').onclick=()=>setMode('last');
document.getElementById('b-raw').onclick=()=>setMode('raw');
function sendKeys(payload){
  if(!sel) return;
  fetch('/api/keys/'+encodeURIComponent(sel),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).catch(()=>{});
}
document.getElementById('send').onclick=()=>{
  const inp=document.getElementById('intext'); const t=inp.value; inp.value='';
  if(t) sendKeys({text:t, enter:true});
};
document.getElementById('intext').addEventListener('keydown',e=>{
  // plain Enter is a newline; only ⌘⏎ / Ctrl+⏎ submits. Ignore Enter while
  // an IME composition is in flight — that Enter confirms the candidate.
  if(e.key==='Enter' && (e.metaKey||e.ctrlKey) && !e.isComposing){e.preventDefault();document.getElementById('send').click();}
});
document.querySelectorAll('.sk').forEach(b=>b.onclick=()=>sendKeys({key:b.dataset.key}));
async function refresh(){
  if(paused) return;
  try{
    const d = await (await fetch('/api/window/'+KEY+'?lines=300')).json();
    panes=d.panes;
    if(!panes.some(p=>p.id===sel)) sel=panes[0]?.id||null;
    document.getElementById('meta').textContent =
      `${d.panes.length}p · ${d.state} · ${new Date().toLocaleTimeString()}`;
    render();
  }catch(e){document.getElementById('meta').textContent='error: '+e;}
}
refresh(); setInterval(refresh, 2000);
</script></body></html>"""


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj).encode(), "application/json; charset=utf-8", code)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self._send(OVERVIEW_HTML.encode(), "text/html; charset=utf-8")
            if p == "/api/overview":
                return self._json(collect_overview())
            if p.startswith("/w/"):
                parts = [unquote(x) for x in p[len("/w/"):].split("/")]
                if len(parts) != 2:
                    return self._json({"error": "bad path"}, 400)
                session, index = parts
                try:
                    w = collect_window(session, index, 0)
                except RuntimeError as e:
                    return self._json({"error": str(e)}, 404)
                return self._send(detail_html(session, index, w["name"]).encode(),
                                  "text/html; charset=utf-8")
            if p.startswith("/api/window/"):
                parts = [unquote(x) for x in p[len("/api/window/"):].split("/")]
                if len(parts) != 2:
                    return self._json({"error": "bad path"}, 400)
                lines = int(q.get("lines", ["250"])[0])
                return self._json(collect_window(parts[0], parts[1], lines))
            if p == "/api/state":
                lines = int(q.get("lines", ["80"])[0])
                return self._json(collect_state_legacy(lines))
            if p.startswith("/api/pane/"):
                pid = p[len("/api/pane/"):]
                lines = int(q.get("lines", ["80"])[0])
                return self._json({"id": pid, "content": capture(pid, lines, True)})
            self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        p = urlparse(self.path).path
        if not p.startswith("/api/keys/"):
            return self._json({"error": "not found"}, 404)
        pane = unquote(p[len("/api/keys/"):])
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n) if n else b"{}")
        except Exception:
            data = {}
        try:
            if "text" in data:
                print(f"[keys] pane={pane} enter={data.get('enter', True)} "
                      f"text={str(data['text'])!r}", flush=True)
                _tmux(["send-keys", "-t", pane, "-l", str(data["text"])])
                if data.get("enter", True):
                    # text + Enter in one burst looks like a paste to TUI
                    # agents (paste detection swallows the Enter / turns it
                    # into a newline). Pace the Enter like a human keystroke.
                    time.sleep(ENTER_DELAY)
                    _tmux(["send-keys", "-t", pane, "Enter"])
            elif "key" in data:
                print(f"[keys] pane={pane} key={data['key']!r}", flush=True)
                _tmux(["send-keys", "-t", pane, str(data["key"])])
            else:
                return self._json({"error": "need 'text' or 'key'"}, 400)
        except RuntimeError as e:
            return self._json({"error": str(e)}, 500)
        return self._json({"ok": True, "pane": pane})

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")


def collect_state_legacy(lines: int = 80) -> dict:
    try:
        out = _tmux(["list-panes", "-a", "-F", PANE_FMT])
    except RuntimeError:
        out = ""
    sessions: dict[str, dict] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        p = _parse(line)
        if not p:
            continue
        content = capture(p["pid"], lines, escapes=True)
        pv = _pane_view(p, _classify(p), content, True)
        s = sessions.setdefault(p["session"], {"name": p["session"], "_w": {}})
        w = s["_w"].setdefault(p["windex"], {"index": p["windex"],
                                             "name": p["wname"], "panes": []})
        w["panes"].append(pv)
    result, nw, np_ = [], 0, 0
    for sn in sorted(sessions):
        s = sessions[sn]
        wins = [s["_w"][k] for k in sorted(s["_w"], key=int)]
        nw += len(wins); np_ += sum(len(w["panes"]) for w in wins)
        result.append({"name": sn, "windows": wins})
    return {"sessions": result, "windows": nw, "panes": np_}


def primary_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default 127.0.0.1). 0.0.0.0 exposes full terminal "
        "content on the LAN with no authentication — only use on a trusted network",
    )
    ap.add_argument("--port", type=int, default=20010)
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("tmux viz serving on:")
    print(f"  http://127.0.0.1:{a.port}/        (overview)")
    if a.host == "0.0.0.0":
        ip = primary_ip()
        print(f"  http://{ip}:{a.port}/        (LAN — UNAUTHENTICATED, terminal content visible)")
    print(f"  /w/<session>/<index>  (detail)")
    print("Ctrl-C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
