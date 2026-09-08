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
    print(f"Prax Bot web interface: {url}", flush=True)
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
:root{
--bg:#0b0e0d;--surface:#111514;--elev:#161b19;--border:#232b28;--border-soft:#1b211f;--hover:#1a201e;
--text:#e7ece9;--muted:#8b978f;--faint:#616b66;
--accent:#43d693;--accent-soft:rgba(67,214,147,.13);--accent-line:rgba(67,214,147,.42);
--add:#7ee787;--add-bg:rgba(63,185,80,.16);--del:#ff9a94;--del-bg:rgba(248,81,73,.15);--hunk:#79c0ff;--meta:#7d8a83;
--warn:#e3b341;--bad:#ff7b72;
--radius:11px;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 var(--sans);display:flex;flex-direction:column;-webkit-font-smoothing:antialiased}
*::-webkit-scrollbar{width:10px;height:10px}
*::-webkit-scrollbar-thumb{background:var(--border);border-radius:6px;border:2px solid transparent;background-clip:padding-box}
*::-webkit-scrollbar-thumb:hover{background:#2c3531;background-clip:padding-box}
.dim{color:var(--faint);font-size:12.5px;padding:4px 6px}

header{display:flex;align-items:center;gap:11px;padding:10px 18px;border-bottom:1px solid var(--border);background:var(--surface);flex:none}
.logo{width:27px;height:27px;border-radius:8px;background:linear-gradient(140deg,#4ff0a3,#1c8f5f);display:grid;place-items:center;color:#03130b;font:800 15px/1 var(--mono);box-shadow:0 2px 10px rgba(67,214,147,.32)}
header .name{font:700 14.5px var(--sans);letter-spacing:.01em}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:20px;background:var(--bg);border:1px solid var(--border-soft);color:var(--text);font-size:12px;max-width:340px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.chip .k{color:var(--faint)}
header .spacer{flex:1}
header label{color:var(--faint);font-size:11px;display:flex;align-items:center;gap:7px}
.seg{display:inline-flex;background:var(--bg);border:1px solid var(--border-soft);border-radius:9px;padding:2px}
.seg button{border:none;background:none;color:var(--muted);padding:5px 11px;border-radius:7px;font:500 12px var(--sans);cursor:pointer;transition:.14s}
.seg button:hover{color:var(--text)}
.seg button.on{background:var(--elev);color:var(--text);box-shadow:0 1px 2px rgba(0,0,0,.35)}

main{flex:1;display:flex;min-height:0;background:radial-gradient(1100px 380px at 50% -12%,rgba(67,214,147,.05),transparent)}
aside{width:272px;flex:none;border-right:1px solid var(--border);padding:14px 12px;overflow:auto;background:var(--surface)}
.slabel{font:600 10.5px/1 var(--sans);letter-spacing:.09em;text-transform:uppercase;color:var(--faint);margin:20px 4px 8px}
.slabel:first-child{margin-top:2px}
.path{font:11.5px/1.5 var(--mono);color:var(--muted);padding:0 4px;word-break:break-all}
.frow{display:flex;align-items:center;gap:8px;padding:4px 6px;border-radius:7px;font:12px var(--mono);color:var(--text)}
.frow:hover{background:var(--hover)}.frow .g{color:var(--accent);font-size:9px}
.rround{display:flex;gap:9px;align-items:baseline;padding:5px 6px;border-radius:7px;color:var(--muted);cursor:pointer;font-size:12.5px;transition:.12s}
.rround:hover{background:var(--hover);color:var(--text)}.rround .n{color:var(--faint);font:11px var(--mono)}
.todo{display:flex;gap:8px;padding:3px 6px;font-size:12.5px;color:var(--text)}
.todo.completed{color:var(--faint);text-decoration:line-through}.todo .tb{color:var(--accent)}.todo.pending .tb{color:var(--faint)}

.content{flex:1;overflow:auto}
#chat,#traj{max-width:940px;margin:0 auto;padding:26px 24px 40px}
#traj{display:none}#traj.on{display:block}#chat{display:none}#chat.on{display:block}
#empty{text-align:center;padding:76px 20px;color:var(--faint)}
#empty h2{color:var(--text);font:600 17px var(--sans);margin:0 0 8px}
#empty p{margin:0;color:var(--muted)}
#empty .hint{display:inline-flex;flex-direction:column;gap:6px;margin-top:22px;text-align:left;font:12.5px var(--mono);color:var(--faint);background:var(--surface);border:1px solid var(--border-soft);border-radius:10px;padding:14px 16px}
.turn{margin-bottom:24px;animation:in .2s ease}
.user{position:relative;background:var(--surface);border:1px solid var(--border-soft);border-left:2px solid var(--accent);border-radius:var(--radius);padding:11px 15px;white-space:pre-wrap;font-size:14px}
.group,.diff{margin-top:10px;border:1px solid var(--border-soft);border-radius:var(--radius);overflow:hidden;background:var(--surface)}
.ghead,.diffhead{width:100%;text-align:left;background:none;border:none;color:var(--muted);padding:9px 13px;cursor:pointer;font:500 12.5px var(--sans);display:flex;align-items:center;gap:8px}
.ghead:hover,.diffhead:hover{color:var(--text)}
.chev{display:inline-block;transition:transform .16s;color:var(--faint)}
.group.open .chev,.diff.open .chev{transform:rotate(90deg)}
.rows{display:none;border-top:1px solid var(--border-soft)}.group.open .rows{display:block}
.row{padding:7px 13px;border-top:1px solid var(--border-soft);font:12px/1.55 var(--mono);cursor:pointer;transition:background .12s;animation:in .16s ease}
.row:first-child{border-top:none}.row:hover{background:var(--hover)}
.row .name{color:var(--accent);font-weight:600}.row .args{color:var(--text)}.row .res{color:var(--muted)}
.row.err{box-shadow:inset 2px 0 0 var(--bad)}.row.err .res{color:var(--bad)}
.row pre{display:none;white-space:pre-wrap;margin:8px 0 2px;padding:10px;background:var(--bg);border:1px solid var(--border-soft);border-radius:8px;color:var(--text);max-height:340px;overflow:auto;font-size:11.5px}
.row.open pre{display:block}
.think{color:var(--muted);font-style:italic;margin:9px 2px;white-space:pre-wrap;font-size:12.5px;border-left:2px solid var(--border);padding-left:11px}
.summary{margin-top:11px;padding:12px 15px 12px 40px;position:relative;background:var(--surface);border:1px solid var(--accent-line);border-radius:var(--radius);white-space:pre-wrap;font-size:13.5px}
.summary:before{content:"✓";position:absolute;left:15px;top:11px;color:var(--accent);font-weight:800}
.summary.stopped{border-color:rgba(227,179,65,.42)}.summary.stopped:before{content:"!";color:var(--warn)}
.stats{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}
.stats span{font-size:11.5px;color:var(--muted);background:var(--surface);border:1px solid var(--border-soft);padding:2px 9px;border-radius:20px}
.difflines{display:none;overflow-x:auto;border-top:1px solid var(--border-soft);padding:7px 0;background:var(--bg)}
.diff.open .difflines{display:block}
.dl{font:12px/1.55 var(--mono);white-space:pre;padding:0 13px;color:var(--text)}
.dl.add{color:var(--add);background:var(--add-bg)}.dl.del{color:var(--del);background:var(--del-bg)}.dl.hunk{color:var(--hunk)}.dl.meta{color:var(--meta)}
.log{font:12px/1.65 var(--mono);white-space:pre-wrap;color:var(--muted);padding:1px 0}.log.bad{color:var(--bad)}

footer{border-top:1px solid var(--border);background:linear-gradient(180deg,var(--surface),var(--bg));padding:14px 0 12px;flex:none}
.wrap{max-width:940px;margin:0 auto;padding:0 24px}
.composer{display:flex;gap:8px;align-items:flex-end;position:relative;background:var(--elev);border:1px solid var(--border);border-radius:15px;padding:8px 8px 8px 15px;transition:.15s}
.composer:focus-within{border-color:var(--accent-line);box-shadow:0 0 0 3px var(--accent-soft)}
textarea{flex:1;background:none;border:none;color:var(--text);font:14px/1.5 var(--sans);resize:none;min-height:26px;max-height:180px;padding:6px 0}
textarea:focus{outline:none}textarea::placeholder{color:var(--faint)}
.btn{border-radius:9px;padding:9px 15px;font:600 13px/1 var(--sans);cursor:pointer;border:1px solid var(--border);background:var(--bg);color:var(--text);transition:.14s;flex:none}
.btn:hover{background:var(--hover);border-color:var(--accent-line)}
.btn.primary{background:var(--accent);color:#03130b;border-color:transparent}.btn.primary:hover{filter:brightness(1.07)}
.btn:disabled{opacity:.38;cursor:default}.btn:active{transform:translateY(1px)}
#menu{position:absolute;bottom:calc(100% + 8px);left:0;right:0;background:var(--elev);border:1px solid var(--border);border-radius:12px;box-shadow:0 14px 44px rgba(0,0,0,.55);overflow:hidden;display:none;z-index:5}
#menu.on{display:block}
#menu div{padding:9px 15px;cursor:pointer;display:flex;gap:14px;align-items:baseline}
#menu div.sel{background:var(--accent-soft)}
#menu b{font:600 13px var(--mono);color:var(--accent);min-width:78px}#menu span{color:var(--muted);font-size:12.5px}
.statusbar{max-width:940px;margin:11px auto 0;padding:0 26px;display:flex;align-items:center;gap:14px;color:var(--muted);font-size:12px}
.statusbar .grow{flex:1}
.dot{width:8px;height:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);flex:none}
.dot.working{background:var(--warn);box-shadow:0 0 0 3px rgba(227,179,65,.22);animation:pulse 1.1s ease-in-out infinite}
.dot.failed{background:var(--bad);box-shadow:0 0 0 3px rgba(255,123,114,.2)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.32}}
@keyframes in{from{opacity:0;transform:translateY(-3px)}to{opacity:1;transform:none}}
@media(max-width:780px){aside{display:none}}
</style></head><body>
<header>
<div class="logo">&#10095;</div><span class="name">Prax Bot</span>
<span class="chip"><span id="hrepo"></span></span>
<span class="chip"><span class="k">backend</span><span id="hbackend"></span></span>
<span class="chip"><span class="k">model</span><span id="hmodel"></span></span>
<span class="spacer"></span>
<label>effort <span id="effort" class="seg"><button data-v="off">off</button><button data-v="low">low</button><button data-v="high">high</button><button data-v="xhigh">xhigh</button></span></label>
<span class="seg"><button id="tabchat" class="on">Chat</button><button id="tabtraj">Trajectory</button></span>
</header>
<main>
<aside>
<div class="slabel">Session</div><div id="sinfo" class="path"></div>
<div class="slabel">Changes</div><div id="sfiles"><div class="dim">none pending</div></div>
<div class="slabel">To-dos</div><div id="stodos"><div class="dim">none</div></div>
<div class="slabel">Rounds</div><div id="srounds"><div class="dim">none yet</div></div>
</aside>
<div class="content">
<section id="chat" class="on">
<div id="empty"><h2>Ready when you are</h2><p>Describe a change and press Enter. Prax Bot works in a sandbox and shows you the diff.</p>
<div class="hint"><span>fix the failing test in tests/test_cache.py</span><span>/goal all tests pass&nbsp;&nbsp;--check "pytest -q"</span><span>/apply&nbsp;&nbsp;/undo&nbsp;&nbsp;/diff&nbsp;&nbsp;/effort&nbsp;&nbsp;/help</span></div></div>
</section>
<section id="traj"></section>
</div>
</main>
<footer>
<div class="wrap"><div class="composer">
<div id="menu"></div>
<textarea id="input" rows="1" placeholder="Describe a change, or type / for commands"></textarea>
<button id="apply" class="btn" title="/apply">Apply</button><button id="undo" class="btn" title="/undo">Undo</button><button id="send" class="btn primary">Send</button>
</div></div>
<div class="statusbar"><span id="dot" class="dot"></span><span id="sstatus">starting</span><span class="grow"></span><span id="sround"></span><span id="stoks"></span><span id="spend"></span><span id="squeue"></span></div>
</footer>
<script>
const TOKEN="__TOKEN__";const $=s=>document.querySelector(s);
const COMMANDS=[["/apply","apply the pending changes to your working tree"],["/undo","undo the last instruction"],["/diff","show the pending changes"],["/goal","work in rounds: /goal [--check CMD] [--rounds N] OBJECTIVE"],["/resume","swap in a past run's checkpoint: /resume RUN-ID [--from N]"],["/effort","off, low, high or xhigh for the next instructions"],["/status","steps, tokens, checkpoints, pending changes"],["/help","list the commands"],["/quit","end the session"]];
let state={},turn=null,group=null,callRows={},roundStepsStart=0;
const chat=$("#chat"),traj=$("#traj"),input=$("#input"),menu=$("#menu");
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e}
function describe(name,a){try{const o=JSON.parse(a||"{}");if(o.command)return o.command;if(o.path){let s=o.path;if(o.start_line)s+=":"+o.start_line+(o.end_line?"-"+o.end_line:"");return s}if(o.pattern)return o.pattern+(o.path?" in "+o.path:"");if(o.name)return o.name;if(o.summary)return o.summary;if(o.step!==undefined)return "→ step "+o.step;return JSON.stringify(o).slice(0,120)}catch(e){return String(a).slice(0,120)}}
function newTurn(text){$("#empty").style.display="none";turn=el("div","turn");turn.dataset.label=text.slice(0,34);turn.appendChild(el("div","user",text));group=null;chat.appendChild(turn);turn.scrollIntoView({block:"end",behavior:"smooth"})}
function ensureGroup(){if(!turn)newTurn("(continued)");if(!group){group=el("div","group open");const b=el("button","ghead");b.innerHTML='<span class="chev">▸</span><span class="gcount">0 tool calls</span>';b.onclick=()=>group.classList.toggle("open");const box=el("div","rows");group.appendChild(b);group.appendChild(box);turn.appendChild(group)}return group}
function addCall(e){const g=ensureGroup();const r=el("div","row");r.innerHTML='<span class="name"></span> <span class="args"></span> <span class="res"></span><pre></pre>';r.querySelector(".name").textContent=e.name;r.querySelector(".args").textContent=describe(e.name,e.args);r.onclick=()=>r.classList.toggle("open");g.querySelector(".rows").appendChild(r);callRows[e.step+":"+e.name]=r;const n=g.querySelectorAll(".row").length;g.querySelector(".gcount").textContent=n+" tool call"+(n>1?"s":"");chat.scrollTop=chat.scrollHeight}
function addResult(e){const r=callRows[e.step+":"+e.name];if(!r)return;const out=String(e.output||"");r.querySelector(".res").textContent="→ "+out.split("\n")[0].slice(0,120);if(out.startsWith("error:"))r.classList.add("err");r.querySelector("pre").textContent=out}
function addSummary(e,finished){if(!turn)newTurn("(continued)");turn.appendChild(el("div","summary"+(finished?"":" stopped"),e.summary||"(no summary)"));if(group)group.classList.remove("open");chat.scrollTop=chat.scrollHeight}
function addStats(s,rs){if(!turn)return;const w=el("div","stats");[rs+" steps this round",s.steps+" steps total",s.tokens.toLocaleString()+" tokens",s.checkpoints+" checkpoints","cost "+s.cost.toFixed(4)].forEach(x=>w.appendChild(el("span","",x)));turn.appendChild(w)}
function addDiff(diff){if(!turn||!diff)return;const files=diff.split("\n").filter(l=>l.startsWith("diff --git")).length;const d=el("div","diff");const b=el("button","diffhead");b.innerHTML='<span class="chev">▸</span> '+files+' file'+(files>1?"s":"")+' changed';b.onclick=()=>d.classList.toggle("open");const box=el("div","difflines");diff.split("\n").forEach(l=>{let c="ctx";if(l.startsWith("+++")||l.startsWith("---")||l.startsWith("diff ")||l.startsWith("index "))c="meta";else if(l.startsWith("@@"))c="hunk";else if(l.startsWith("+"))c="add";else if(l.startsWith("-"))c="del";box.appendChild(el("div","dl "+c,l||" "))});d.appendChild(b);d.appendChild(box);turn.appendChild(d)}
function renderState(s){state=s;$("#hrepo").textContent=s.name;$("#hbackend").textContent=s.backend;$("#hmodel").textContent=s.model;
document.querySelectorAll("#effort button").forEach(b=>b.classList.toggle("on",b.dataset.v===s.effort));
$("#sinfo").textContent=s.repo;
const sf=$("#sfiles");sf.innerHTML="";if(s.files.length){s.files.forEach(f=>{const r=el("div","frow");r.innerHTML='<span class="g">●</span>';r.appendChild(document.createTextNode(f));sf.appendChild(r)})}else sf.innerHTML='<div class="dim">none pending</div>';
const st=$("#sstatus"),dot=$("#dot");st.textContent=s.status==="working"?("working"+(s.step?" · "+s.step:"")):s.status;dot.className="dot"+(s.status==="working"?" working":s.status==="failed"?" failed":"");
$("#sround").textContent=s.rounds+" rounds · "+s.steps+" steps";$("#stoks").textContent=s.tokens.toLocaleString()+" tokens · "+s.calls+" calls";$("#spend").textContent=s.files.length?s.files.length+" files pending":"nothing pending";$("#squeue").textContent=s.queued?s.queued+" queued":"";
$("#send").disabled=s.closed||s.status==="failed";$("#apply").disabled=s.closed||!s.files.length;$("#undo").disabled=s.closed||!s.rounds;
if(s.closed){st.textContent="session ended";input.disabled=true}}
function renderRounds(){const r=$("#srounds");r.innerHTML="";const turns=[...document.querySelectorAll(".turn")];if(!turns.length){r.innerHTML='<div class="dim">none yet</div>';return}turns.forEach((t,i)=>{const d=el("div","rround");d.innerHTML='<span class="n">'+(i+1)+'</span>';d.appendChild(document.createTextNode(t.dataset.label||""));d.onclick=()=>t.scrollIntoView({block:"start",behavior:"smooth"});r.appendChild(d)})}
function renderTodos(list){const t=$("#stodos");t.innerHTML="";if(!list||!list.length){t.innerHTML='<div class="dim">none</div>';return}list.forEach(x=>{const d=el("div","todo "+(x.status||""));d.appendChild(el("span","tb",x.status==="completed"?"✓":x.status==="in_progress"?"◐":"○"));d.appendChild(document.createTextNode(x.content));t.appendChild(d)})}
function addLog(text){traj.appendChild(el("div","log"+(text.startsWith("✗")||text.includes("error")?" bad":""),text));if(text.startsWith("✗")||text.startsWith("could not"))chat.appendChild(el("div","log bad",text))}
function handle(e){switch(e.kind){
case "instruction.start":newTurn(e.text);roundStepsStart=state.steps||0;renderRounds();break;
case "tool.call":addCall(e);break;
case "tool.result":addResult(e);break;
case "assistant":if(e.text&&e.text.trim()){ensureGroup();turn.appendChild(el("div","think",e.text.trim().slice(0,600)))}break;
case "finish":addSummary(e,true);break;
case "todo":renderTodos(e.todos);break;
case "instruction.done":if(group)group.classList.remove("open");break;
case "state":{const pr=state.rounds||0;renderState(e);if(e.rounds>pr&&turn&&!turn.querySelector(".stats")){addStats(e,e.steps-roundStepsStart);addDiff(e.diff)}break}
case "queued":break;
case "log":addLog(e.text);break;
case "goal.round":addLog("goal round "+e.round+(e.met?" · goal met":e.stopped?" · "+e.stopped:""));break;
case "sandbox.retry":addLog("sandbox retry: "+e.error);break;
default:if(e.kind&&e.kind.startsWith("sandbox"))addLog(e.kind.replace("sandbox.","sandbox: ")+(e.image?" "+e.image:"")+(e.elapsed?" · "+e.elapsed.toFixed(1)+"s":""))}}
function connect(){const es=new EventSource("/events?token="+TOKEN);es.onmessage=m=>{try{handle(JSON.parse(m.data))}catch(err){console.error(err)}};es.onerror=()=>{es.close();setTimeout(connect,1500)}}
async function send(text){if(!text.trim())return;input.value="";autosize();menu.classList.remove("on");await fetch("/send?token="+TOKEN,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text})})}
$("#send").onclick=()=>send(input.value);$("#apply").onclick=()=>send("/apply");$("#undo").onclick=()=>send("/undo");
document.querySelectorAll("#effort button").forEach(b=>b.onclick=()=>send("/effort "+b.dataset.v));
$("#tabchat").onclick=()=>{chat.classList.add("on");traj.classList.remove("on");$("#tabchat").classList.add("on");$("#tabtraj").classList.remove("on")};
$("#tabtraj").onclick=()=>{traj.classList.add("on");chat.classList.remove("on");$("#tabtraj").classList.add("on");$("#tabchat").classList.remove("on")};
let menuSel=0;
function matches(){return COMMANDS.filter(c=>c[0].slice(1).startsWith(input.value.slice(1).toLowerCase()))}
function showMenu(){const v=input.value;if(!v.startsWith("/")||v.includes(" ")){menu.classList.remove("on");return}const items=matches();menu.innerHTML="";if(!items.length){menu.classList.remove("on");return}menuSel=Math.min(menuSel,items.length-1);items.forEach((c,i)=>{const d=el("div",i===menuSel?"sel":"");d.innerHTML="<b></b><span></span>";d.querySelector("b").textContent=c[0];d.querySelector("span").textContent=c[1];d.onmousedown=ev=>{ev.preventDefault();input.value=c[0]+" ";menu.classList.remove("on");input.focus()};menu.appendChild(d)});menu.classList.add("on")}
input.addEventListener("input",()=>{menuSel=0;showMenu();autosize()});
input.addEventListener("keydown",ev=>{if(menu.classList.contains("on")){const items=menu.querySelectorAll("div");if(ev.key==="ArrowDown"){menuSel=(menuSel+1)%items.length;showMenu();ev.preventDefault();return}if(ev.key==="ArrowUp"){menuSel=(menuSel-1+items.length)%items.length;showMenu();ev.preventDefault();return}if((ev.key==="Tab"||ev.key==="Enter")&&items.length){input.value=matches()[menuSel][0]+" ";menu.classList.remove("on");ev.preventDefault();return}if(ev.key==="Escape"){menu.classList.remove("on");return}}
if(ev.key==="Enter"&&!ev.shiftKey){ev.preventDefault();send(input.value)}});
function autosize(){input.style.height="auto";input.style.height=Math.min(input.scrollHeight,180)+"px"}
input.focus();connect();
</script></body></html>
"""
