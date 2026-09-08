"""A local web interface on the session: one page, events streamed to it, instructions posted back.

The server binds to localhost only. Every event the agent emits is forwarded to the page as JSON;
the page renders instructions, tool calls, results, summaries and diffs, and offers the slash commands.
"""

from __future__ import annotations

import json
import queue
import secrets
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from rich.console import Console
from rich.text import Text

from .settings import Settings

HISTORY_LIMIT = 5000


class LineSink:
    """A file-like object for a Rich console; each complete line becomes a `log` event."""

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self.emit = emit
        self.buffer = ""
        self.lock = threading.Lock()

    def write(self, text: str) -> int:
        with self.lock:
            self.buffer += text
            lines = self.buffer.split("\n")
            self.buffer = lines.pop()
        for line in lines:
            plain = Text.from_ansi(line).plain.rstrip()
            if plain:
                self.emit({"kind": "log", "text": plain})
        return len(text)

    def flush(self) -> None:
        with self.lock:
            pending, self.buffer = self.buffer, ""
        plain = Text.from_ansi(pending).plain.rstrip()
        if plain:
            self.emit({"kind": "log", "text": plain})

    def isatty(self) -> bool:
        return False


class WebSession:
    """Owns the session, a worker that runs typed lines one at a time, and the event stream."""

    def __init__(self, settings: Settings, root: Path, backend: str, make_session: Callable[[Console, Callable[..., None]], Any]) -> None:
        self.settings, self.root, self.backend = settings, root, backend
        self.make_session = make_session
        self.session: Any = None
        self.out = Console(file=LineSink(self.emit), force_terminal=False, width=110)
        self.history: list[dict[str, Any]] = []
        self.clients: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.pending: queue.Queue[str] = queue.Queue()
        self.busy = False
        self.closed = False
        self.error: str | None = None
        self.current_step = ""
        self.seq = 0
        threading.Thread(target=self._worker, name="praxbot-web-worker", daemon=True).start()

    # ---- events ----
    def emit(self, event: dict[str, Any]) -> None:
        with self.lock:
            self.seq += 1
            event = {"seq": self.seq, "t": time.time(), **event}
            self.history.append(event)
            if len(self.history) > HISTORY_LIMIT:
                del self.history[: len(self.history) - HISTORY_LIMIT]
            clients = list(self.clients)
        for q in clients:
            q.put(event)

    def hook(self, kind: str, **payload: Any) -> None:
        """Wrapped around the session's event callback: every agent event reaches the page."""
        safe = {k: _json_safe(v) for k, v in payload.items()}
        if kind == "tool.call":
            self.current_step = f"step {payload.get('step')} · {payload.get('name')}"
        self.emit({"kind": kind, **safe})
        if kind in ("tool.call", "tool.result", "finish", "sandbox.ready", "goal.round"):
            self.emit(self.state_event())

    def state(self) -> dict[str, Any]:
        s = self.session
        diff = (s.last_diff if s else "") or ""
        files = [line.split(" b/", 1)[-1] for line in diff.splitlines() if line.startswith("diff --git ")]
        status = "starting" if s is None and not self.error else "failed" if self.error else "working" if self.busy else "idle"
        return {
            "status": status,
            "step": self.current_step if self.busy else "",
            "queued": self.pending.qsize(),
            "repo": str(self.root),
            "name": self.root.name,
            "backend": self.backend,
            "model": (s.settings.model if s else self.settings.model),
            "effort": (s.settings.effort if s else self.settings.effort),
            "rounds": s.rounds if s else 0,
            "steps": s.steps if s else 0,
            "tokens": s.llm.usage.total_tokens if s else 0,
            "calls": s.llm.usage.calls if s else 0,
            "checkpoints": len(s.ws.checkpoints) if s else 0,
            "cost": s.ws.total_cost if s else 0.0,
            "files": files,
            "diff": diff,
            "summary": s.last_summary if s else "",
            "applied_rounds": s.applied_rounds if s else 0,
            "log": str(s.run_dir) if s else "",
            "closed": self.closed,
            "error": self.error,
        }

    def state_event(self) -> dict[str, Any]:
        return {"kind": "state", **self.state()}

    def subscribe(self) -> tuple[list[dict[str, Any]], queue.Queue]:
        q: queue.Queue = queue.Queue()
        with self.lock:
            history = list(self.history)
            self.clients.append(q)
        return history, q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    # ---- work ----
    def submit(self, line: str) -> None:
        line = line.strip()
        if line and not self.closed:
            self.pending.put(line)
            self.emit({"kind": "queued", "text": line, **{"queued": self.pending.qsize()}})

    def _worker(self) -> None:
        try:
            self.session = self.make_session(self.out, self.hook)
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {str(e)[:300]}"
            self.emit({"kind": "log", "text": f"could not start: {self.error}"})
            self.emit(self.state_event())
            return
        self.emit(self.state_event())
        while not self.closed:
            line = self.pending.get()
            self.busy = True
            self.emit({"kind": "instruction.start", "text": line})
            self.emit(self.state_event())
            keep_going = True
            try:
                keep_going = self.session.handle_line(line)
            except Exception as e:  # noqa: BLE001
                self.emit({"kind": "log", "text": f"✗ {type(e).__name__}: {str(e)[:300]}"})
            self.busy = False
            self.emit({"kind": "instruction.done", "text": line})
            self.emit(self.state_event())
            if not keep_going:
                self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.session is not None:
            try:
                self.session.close()
            except Exception:  # noqa: BLE001
                pass
        self.emit(self.state_event())


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if hasattr(value, "__dict__"):
        return {k: _json_safe(v) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def make_handler(web: WebSession, token: str, page: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet by design
            pass

        def _authorized(self) -> bool:
            return self.headers.get("X-Prax-Token") == token or parse_qs(urlparse(self.path).query).get("token", [""])[0] == token

        def _json(self, status: int, body: Any) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                data = page.replace("__TOKEN__", token).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == "/state":
                self._json(HTTPStatus.OK, web.state())
            elif path == "/events":
                if not self._authorized():
                    self._json(HTTPStatus.FORBIDDEN, {"error": "bad token"})
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                history, q = web.subscribe()
                try:
                    for event in history:
                        self._send_event(event)
                    self._send_event(web.state_event())
                    while True:
                        try:
                            event = q.get(timeout=15)
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        self._send_event(event)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    web.unsubscribe(q)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _send_event(self, event: dict[str, Any]) -> None:
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
            self.wfile.flush()

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._json(HTTPStatus.FORBIDDEN, {"error": "bad token"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "bad json"})
                return
            path = urlparse(self.path).path
            if path == "/send":
                text = str(body.get("text") or "")
                if text.strip().lower() in ("/quit", "/exit", "/q"):
                    web.submit(text)
                else:
                    web.submit(text)
                self._json(HTTPStatus.OK, {"ok": True, "queued": web.pending.qsize()})
            elif path == "/quit":
                web.close()
                self._json(HTTPStatus.OK, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    return Handler


def serve(web: WebSession, *, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True, page: str | None = None) -> None:
    """Serve the page until /quit or an interrupt. Port 0 picks a free port."""
    token = secrets.token_urlsafe(16)
    server = ThreadingHTTPServer((host, port), make_handler(web, token, page or PAGE))
    server.daemon_threads = True
    url = f"http://{host}:{server.server_address[1]}/?token={token}"
    print(f"Prax Bot web interface: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        web.close()
        server.server_close()


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Prax Bot</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#0f1412;--panel:#161d1a;--line:#26302c;--text:#dfe6e2;--muted:#8a9690;--accent:#4cc38a;--warn:#e0b04a;--bad:#e06c5a;--user:#1c2a24}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;height:100vh;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:14px;padding:8px 14px;border-bottom:1px solid var(--line);background:var(--panel)}
header .name{font-weight:600}header .dim{color:var(--muted)}header .tabs{margin-left:auto;display:flex;gap:4px}
header .tabs button{background:none;border:1px solid var(--line);color:var(--muted);padding:4px 10px;border-radius:6px;cursor:pointer}
header .tabs button.on{color:var(--text);border-color:var(--accent)}
main{flex:1;display:flex;min-height:0}
aside{width:260px;border-right:1px solid var(--line);padding:12px;overflow:auto;background:var(--panel);font-size:13px}
aside h3{margin:12px 0 4px;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}aside h3:first-child{margin-top:0}
aside .file{font-family:ui-monospace,Menlo,monospace;font-size:12px}aside .round{cursor:pointer;color:var(--text)}aside .round:hover{color:var(--accent)}
aside .todo{display:flex;gap:6px}aside .todo.done{color:var(--muted);text-decoration:line-through}
#chat,#traj{flex:1;overflow:auto;padding:16px 20px;display:none}#chat.on,#traj.on{display:block}
.turn{margin-bottom:18px}.user{background:var(--user);border-left:3px solid var(--accent);padding:8px 12px;border-radius:0 6px 6px 0;white-space:pre-wrap}
.group{margin:8px 0 0 0;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.group>button{width:100%;text-align:left;background:var(--panel);border:none;color:var(--muted);padding:6px 10px;cursor:pointer;font:inherit}
.group>button:before{content:"▸ ";}.group.open>button:before{content:"▾ ";}
.group .rows{display:none}.group.open .rows{display:block}
.row{padding:5px 10px;border-top:1px solid var(--line);font-family:ui-monospace,Menlo,monospace;font-size:12px;cursor:pointer}
.row .name{color:var(--accent)}.row .args{color:var(--text)}.row .res{color:var(--muted);margin-left:8px}.row.err .res{color:var(--bad)}
.row pre{display:none;white-space:pre-wrap;margin:6px 0 0;color:var(--text);max-height:360px;overflow:auto}.row.open pre{display:block}
.summary{margin-top:10px;padding:8px 12px;border:1px solid var(--accent);border-radius:6px;white-space:pre-wrap}
.summary.stopped{border-color:var(--warn)}
.stats{color:var(--muted);font-size:12px;margin-top:6px}
.diff{margin-top:8px;border:1px solid var(--line);border-radius:6px;overflow:hidden}.diff>button{width:100%;text-align:left;background:var(--panel);border:none;color:var(--muted);padding:6px 10px;cursor:pointer;font:inherit}
.diff pre{display:none;margin:0;padding:8px 10px;font:12px/1.4 ui-monospace,Menlo,monospace;overflow:auto;max-height:480px}.diff.open pre{display:block}
.diff .a{color:#8fd68f}.diff .d{color:#e58b7c}.diff .h{color:#8ab4f8}.diff .m{color:var(--muted)}
.think{color:var(--muted);font-style:italic;margin:6px 0;white-space:pre-wrap}
.log{font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;color:var(--muted)}.log.bad{color:var(--bad)}
footer{border-top:1px solid var(--line);background:var(--panel);padding:10px 14px}
.composer{display:flex;gap:8px;align-items:flex-end;position:relative}
textarea{flex:1;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:10px 12px;font:inherit;resize:none;min-height:44px;max-height:180px}
textarea:focus{outline:none;border-color:var(--accent)}
.composer button{background:var(--accent);color:#0b1410;border:none;border-radius:8px;padding:10px 14px;font-weight:600;cursor:pointer}
.composer button.ghost{background:none;border:1px solid var(--line);color:var(--text);font-weight:500}
.composer button:disabled{opacity:.4;cursor:default}
#menu{position:absolute;bottom:52px;left:0;background:var(--panel);border:1px solid var(--line);border-radius:8px;min-width:360px;display:none;z-index:2}
#menu.on{display:block}#menu div{padding:6px 10px;cursor:pointer}#menu div.sel,#menu div:hover{background:var(--user)}#menu b{font-family:ui-monospace,Menlo,monospace;color:var(--accent)}#menu span{color:var(--muted);margin-left:8px}
.status{display:flex;gap:16px;color:var(--muted);font-size:12px;margin-top:8px}.status .working{color:var(--warn)}.status .idle{color:var(--accent)}
select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:4px 6px}
</style></head><body>
<header><span class="name">Prax Bot</span><span id="hrepo" class="dim"></span><span id="hmodel" class="dim"></span>
<label class="dim">effort <select id="effort"><option>off</option><option>low</option><option>high</option><option>xhigh</option></select></label>
<div class="tabs"><button id="tabchat" class="on">Chat</button><button id="tabtraj">Trajectory</button></div></header>
<main>
<aside><h3>Session</h3><div id="sinfo"></div><h3>Changes</h3><div id="sfiles" class="dim">none pending</div><h3>To-dos</h3><div id="stodos" class="dim">none</div><h3>Rounds</h3><div id="srounds" class="dim">none yet</div></aside>
<section id="chat" class="on"></section><section id="traj"></section>
</main>
<footer>
<div class="composer">
<div id="menu"></div>
<textarea id="input" rows="1" placeholder="An instruction, or / for commands"></textarea>
<button id="apply" class="ghost" title="/apply">Apply</button><button id="undo" class="ghost" title="/undo">Undo</button><button id="send">Send</button>
</div>
<div class="status"><span id="sstatus" class="idle">starting</span><span id="sround"></span><span id="stoks"></span><span id="spend"></span><span id="squeue"></span></div>
</footer>
<script>
const TOKEN="__TOKEN__";const $=s=>document.querySelector(s);
const COMMANDS=[["/apply","apply the pending changes to your working tree"],["/undo","put the sandbox back to before the last instruction"],["/diff","show the pending changes"],["/goal","work in rounds: /goal [--check CMD] [--rounds N] OBJECTIVE"],["/resume","swap in a past run's checkpoint: /resume RUN-ID [--from N]"],["/effort","off, low, high or xhigh for the next instructions"],["/status","steps, tokens, checkpoints, pending changes"],["/help","list the commands"],["/quit","end the session"]];
let state={},turn=null,group=null,rows={},lastState={};
const chat=$("#chat"),traj=$("#traj"),input=$("#input");
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e}
function describe(name,args){try{const a=JSON.parse(args||"{}");if(a.command)return a.command;if(a.path){let s=a.path;if(a.start_line)s+=":"+a.start_line+(a.end_line?"-"+a.end_line:"");return s}if(a.pattern)return a.pattern+(a.path?" in "+a.path:"");if(a.name)return a.name;if(a.summary)return a.summary;if(a.step!==undefined)return "step "+a.step;return JSON.stringify(a).slice(0,120)}catch(e){return String(args).slice(0,120)}}
function newTurn(text){turn=el("div","turn");const u=el("div","user",text);turn.appendChild(u);group=null;chat.appendChild(turn);turn.id="turn"+(document.querySelectorAll(".turn").length);turn.scrollIntoView({block:"end"})}
function ensureGroup(){if(!turn)newTurn("(continued)");if(!group){group=el("div","group open");const b=el("button","","0 tool calls");b.onclick=()=>group.classList.toggle("open");group.appendChild(b);group.appendChild(el("div","rows"));turn.appendChild(group)}return group}
function addCall(e){const g=ensureGroup();const r=el("div","row");r.innerHTML='<span class="name"></span> <span class="args"></span><span class="res"></span><pre></pre>';r.querySelector(".name").textContent=e.name;r.querySelector(".args").textContent=describe(e.name,e.args);r.onclick=()=>r.classList.toggle("open");g.querySelector(".rows").appendChild(r);rows[e.step+":"+e.name]=r;const n=g.querySelectorAll(".row").length;g.querySelector("button").textContent=n+" tool call"+(n>1?"s":"");chat.scrollTop=chat.scrollHeight}
function addResult(e){const r=rows[e.step+":"+e.name];if(!r)return;const out=String(e.output||"");const first=out.split("\n")[0].slice(0,140);r.querySelector(".res").textContent=" → "+first;if(out.startsWith("error:"))r.classList.add("err");r.querySelector("pre").textContent=out}
function addSummary(e,finished){if(!turn)newTurn("(continued)");const s=el("div","summary"+(finished?"":" stopped"),e.summary||"(no summary)");turn.appendChild(s);if(group)group.classList.remove("open");chat.scrollTop=chat.scrollHeight}
function addDiff(diff){if(!turn||!diff)return;const d=el("div","diff");const b=el("button","",diff.split("\n").filter(l=>l.startsWith("diff --git")).length+" files changed · click to show the diff");b.onclick=()=>d.classList.toggle("open");const pre=el("pre");pre.innerHTML=diff.split("\n").map(l=>{const c=l.startsWith("+++")||l.startsWith("---")?"h":l.startsWith("+")?"a":l.startsWith("-")?"d":l.startsWith("@@")?"h":l.startsWith("diff ")||l.startsWith("index ")?"m":"";const t=l.replace(/&/g,"&amp;").replace(/</g,"&lt;");return c?'<span class="'+c+'">'+t+'</span>':t}).join("\n");d.appendChild(b);d.appendChild(pre);turn.appendChild(d)}
function addStats(s,roundSteps){if(!turn)return;turn.appendChild(el("div","stats",`${roundSteps} steps this round · ${s.steps} steps · ${s.tokens.toLocaleString()} tokens · ${s.checkpoints} checkpoints · cost ${s.cost.toFixed(4)}`))}
function renderState(s){lastState=state;state=s;$("#hrepo").textContent=s.name+" · "+s.backend;$("#hmodel").textContent=s.model;$("#effort").value=s.effort;
$("#sinfo").textContent=s.repo;$("#sfiles").innerHTML="";if(s.files.length){s.files.forEach(f=>$("#sfiles").appendChild(el("div","file",f)))}else{$("#sfiles").textContent="none pending";$("#sfiles").className="dim"}
const st=$("#sstatus");st.textContent=s.status+(s.step?" · "+s.step:"");st.className=s.status==="working"?"working":"idle";
$("#sround").textContent=s.rounds+" rounds · "+s.steps+" steps";$("#stoks").textContent=s.tokens.toLocaleString()+" tokens in "+s.calls+" calls";$("#spend").textContent=s.files.length?s.files.length+" files pending":"nothing pending";$("#squeue").textContent=s.queued?s.queued+" queued":"";
$("#send").disabled=s.closed||s.status==="failed";$("#apply").disabled=s.closed||!s.files.length;$("#undo").disabled=s.closed||!s.rounds;
if(s.closed){st.textContent="session ended";input.disabled=true}}
function renderRounds(){const r=$("#srounds");r.innerHTML="";const turns=document.querySelectorAll(".turn");if(!turns.length){r.textContent="none yet";r.className="dim";return}r.className="";turns.forEach(t=>{const d=el("div","round",t.querySelector(".user").textContent.slice(0,40));d.onclick=()=>t.scrollIntoView({block:"start"});r.appendChild(d)})}
function renderTodos(list){const t=$("#stodos");t.innerHTML="";if(!list||!list.length){t.textContent="none";t.className="dim";return}t.className="";list.forEach(x=>{const d=el("div","todo"+(x.status==="completed"?" done":""));d.textContent=(x.status==="completed"?"✓ ":x.status==="in_progress"?"▶ ":"○ ")+x.content;t.appendChild(d)})}
function addLog(text){const d=el("div","log"+(text.startsWith("✗")||text.includes("error")?" bad":""),text);traj.appendChild(d);if(text.startsWith("✗")||text.startsWith("could not")){chat.appendChild(el("div","log bad",text))}}
let roundStepsStart=0;
function handle(e){switch(e.kind){
case "instruction.start":newTurn(e.text);roundStepsStart=state.steps||0;renderRounds();break;
case "tool.call":addCall(e);break;
case "tool.result":addResult(e);break;
case "assistant":if(e.text&&e.text.trim()){ensureGroup();turn.appendChild(el("div","think",e.text.trim().slice(0,600)))}break;
case "finish":addSummary(e,true);break;
case "todo":renderTodos(e.todos);break;
case "instruction.done":if(group)group.classList.remove("open");break;
case "state":const prevRounds=state.rounds||0;renderState(e);if(e.rounds>prevRounds&&turn&&!turn.querySelector(".stats")){addStats(e,e.steps-roundStepsStart);addDiff(e.diff)}if(e.status!=="working"&&turn&&e.summary&&!turn.querySelector(".summary")&&e.rounds>prevRounds){}break;
case "queued":break;
case "log":addLog(e.text);break;
case "goal.round":addLog("goal round "+e.round+(e.met?" · goal met":e.stopped?" · "+e.stopped:""));break;
case "sandbox.retry":addLog("sandbox retry: "+e.error);break;
default:if(e.kind&&e.kind.startsWith("sandbox"))addLog(e.kind.replace("sandbox.","sandbox: ")+(e.image?" "+e.image:"")+(e.elapsed?" · "+e.elapsed.toFixed(1)+"s":""));}}
function connect(){const es=new EventSource("/events?token="+TOKEN);es.onmessage=m=>{try{handle(JSON.parse(m.data))}catch(err){console.error(err)}};es.onerror=()=>{es.close();setTimeout(connect,1500)}}
async function send(text){if(!text.trim())return;input.value="";autosize();await fetch("/send?token="+TOKEN,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text})})}
$("#send").onclick=()=>send(input.value);$("#apply").onclick=()=>send("/apply");$("#undo").onclick=()=>send("/undo");
$("#effort").onchange=()=>send("/effort "+$("#effort").value);
$("#tabchat").onclick=()=>{chat.classList.add("on");traj.classList.remove("on");$("#tabchat").classList.add("on");$("#tabtraj").classList.remove("on")};
$("#tabtraj").onclick=()=>{traj.classList.add("on");chat.classList.remove("on");$("#tabtraj").classList.add("on");$("#tabchat").classList.remove("on")};
const menu=$("#menu");let menuSel=0;function showMenu(){const q=input.value.slice(1).toLowerCase();const items=COMMANDS.filter(c=>c[0].slice(1).startsWith(q.split(" ")[0]));menu.innerHTML="";if(!items.length||!input.value.startsWith("/")||input.value.includes(" ")){menu.classList.remove("on");return}menuSel=Math.min(menuSel,items.length-1);items.forEach((c,i)=>{const d=el("div",i===menuSel?"sel":"");d.innerHTML="<b></b><span></span>";d.querySelector("b").textContent=c[0];d.querySelector("span").textContent=c[1];d.onmousedown=ev=>{ev.preventDefault();input.value=c[0]+" ";menu.classList.remove("on");input.focus()};menu.appendChild(d)});menu.classList.add("on")}
input.addEventListener("input",()=>{menuSel=0;showMenu();autosize()});
input.addEventListener("keydown",ev=>{if(menu.classList.contains("on")){const items=menu.querySelectorAll("div");if(ev.key==="ArrowDown"){menuSel=(menuSel+1)%items.length;showMenu();ev.preventDefault();return}if(ev.key==="ArrowUp"){menuSel=(menuSel-1+items.length)%items.length;showMenu();ev.preventDefault();return}if(ev.key==="Tab"||(ev.key==="Enter"&&items.length)){const c=items[menuSel].querySelector("b").textContent;input.value=c+" ";menu.classList.remove("on");ev.preventDefault();return}if(ev.key==="Escape"){menu.classList.remove("on");return}}
if(ev.key==="Enter"&&!ev.shiftKey){ev.preventDefault();send(input.value)}});
function autosize(){input.style.height="auto";input.style.height=Math.min(input.scrollHeight,180)+"px"}
input.focus();connect();
</script></body></html>
"""
