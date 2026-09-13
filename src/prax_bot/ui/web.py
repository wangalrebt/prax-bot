"""A local web interface on the session: one page, events streamed to it, instructions posted back.

The server binds to localhost only. Every event the agent emits is forwarded to the page as JSON;
the page renders instructions, tool calls, results, summaries and diffs, and offers the slash commands.
"""

from __future__ import annotations

import json
import queue
import secrets
import shlex
import threading
import time
import webbrowser
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import IO, Any, cast
from urllib.parse import parse_qs, urlparse

from rich.console import Console
from rich.text import Text

from prax_bot.runtime.runs import json_safe
from prax_bot.settings import Settings
from prax_bot.ui.session import handle_line

HISTORY_LIMIT = 5000

# What the page shows before the session exists; Session.state() supplies every key once it does.
STARTING_STATE: dict[str, Any] = {
    "rounds": 0,
    "steps": 0,
    "tokens": 0,
    "context": 0,
    "calls": 0,
    "checkpoints": 0,
    "cost": 0.0,
    "files": [],
    "excluded": [],
    "scratch": [],
    "binary": [],
    "diff": "",
    "summary": "",
    "applied_rounds": 0,
    "log": "",
    "verification": "not run",
    "checks": [],
}


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

    def __init__(
        self, settings: Settings, root: Path, backend: str, make_session: Callable[[Console, Callable[..., None]], Any]
    ) -> None:
        self.settings, self.root, self.backend = settings, root, backend
        self.make_session = make_session
        self.session: Any = None
        self.out = Console(file=cast(IO[str], LineSink(self.emit)), force_terminal=False, width=110)
        self.history: list[dict[str, Any]] = []
        self.clients: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.pending: queue.Queue[str | None] = queue.Queue()
        self.busy = False
        self.closed = False
        self.closing = False
        self.error: str | None = None
        self.current_step = ""
        self.seq = 0
        self.worker = threading.Thread(target=self._worker, name="praxbot-web-worker", daemon=True)
        self.worker.start()

    # ---- events ----
    def emit(self, event: dict[str, Any]) -> None:
        with self.lock:
            self.seq += 1
            event = {"seq": self.seq, "t": time.time(), **event}
            self.history.append(event)
            if len(self.history) > HISTORY_LIMIT:
                del self.history[: len(self.history) - HISTORY_LIMIT]
            for q in self.clients:
                q.put(event)

    def hook(self, kind: str, **payload: Any) -> None:
        """Wrapped around the session's event callback: every agent event reaches the page."""
        safe = {k: json_safe(v) for k, v in payload.items()}
        if kind == "tool.call":
            self.current_step = f"step {payload.get('step')} · {payload.get('name')}"
        self.emit({"kind": kind, **safe})
        if kind in ("tool.call", "tool.result", "finish", "sandbox.ready", "goal.round"):
            self.emit(self.state_event())

    def state(self) -> dict[str, Any]:
        status = (
            "failed"
            if self.error
            else "closed"
            if self.closed
            else "stopping"
            if self.closing
            else "starting"
            if self.session is None
            else "working"
            if self.busy
            else "idle"
        )
        state = (
            self.session.state()
            if self.session is not None
            else {
                **STARTING_STATE,
                "repo": str(self.root),
                "name": self.root.name,
                "backend": self.backend,
                "model": self.settings.model,
                "effort": self.settings.effort,
                "context_budget": self.settings.context_budget,
            }
        )
        return {
            **state,
            "status": status,
            "step": self.current_step if self.busy else "",
            "queued": self.pending.qsize(),
            "closed": self.closed,
            "error": self.error,
        }

    def state_event(self) -> dict[str, Any]:
        return {"kind": "state", **self.state()}

    def subscribe(self, after: int = 0) -> tuple[list[dict[str, Any]], queue.Queue]:
        q: queue.Queue = queue.Queue()
        with self.lock:
            history = [event for event in self.history if event["seq"] > after]
            self.clients.append(q)
        return history, q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    # ---- work ----
    def submit(self, line: str) -> None:
        line = line.strip()
        if line.lower() == "/stop":
            self.stop()
        elif line and not self.closing and not self.closed:
            self.pending.put(line)
            self.emit({"kind": "queued", "text": line, "queued": self.pending.qsize()})

    def stop(self) -> bool:
        if self.session is None or not self.busy:
            return False
        self.session.request_stop()
        self.emit(self.state_event())
        return True

    def _worker(self) -> None:
        try:
            self.session = self.make_session(self.out, self.hook)
            self.emit(self.state_event())
            while not self.closing:
                line = self.pending.get()
                if line is None or self.closing:
                    break
                self.busy = True
                self.emit({"kind": "instruction.start", "text": line})
                self.emit(self.state_event())
                keep_going = True
                try:
                    keep_going = handle_line(self.session, line, self.out)
                except Exception as error:
                    self.emit({"kind": "log", "text": f"✗ {type(error).__name__}: {str(error)[:500]}"})
                finally:
                    self.busy = False
                self.emit({"kind": "instruction.done", "text": line})
                self.emit(self.state_event())
                if not keep_going:
                    self.closing = True
        except Exception as error:
            self.error = f"{type(error).__name__}: {str(error)[:500]}"
            self.emit({"kind": "log", "text": f"could not start: {self.error}"})
        finally:
            if self.session is not None:
                try:
                    self.session.close()
                except Exception as error:
                    self.emit({"kind": "log", "text": f"✗ while closing: {str(error)[:500]}"})
            self.closed = True
            self.emit(self.state_event())

    def close(self) -> None:
        if self.closing or self.closed:
            return
        self.closing = True
        self.stop()
        self.pending.put(None)
        self.emit(self.state_event())


def make_handler(web: WebSession, token: str, page: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _authorized(self) -> bool:
            return (
                self.headers.get("X-Prax-Token") == token
                or parse_qs(urlparse(self.path).query).get("token", [""])[0] == token
            )

        def _send(self, status: int, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status: int, body: Any) -> None:
            self._send(status, json.dumps(body).encode("utf-8"), "application/json")

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                self._send(HTTPStatus.OK, page.replace("__TOKEN__", token).encode("utf-8"), "text/html; charset=utf-8")
            elif path in ("/assets/app.css", "/assets/app.js"):
                name = path.rsplit("/", 1)[-1]
                content_type = "text/css" if name.endswith(".css") else "text/javascript"
                self._send(HTTPStatus.OK, (ASSETS / name).read_bytes(), content_type)
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
                try:
                    after = int(
                        self.headers.get("Last-Event-ID") or parse_qs(urlparse(self.path).query).get("after", ["0"])[0]
                    )
                except ValueError:
                    after = 0
                history, q = web.subscribe(after)
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
            if "seq" in event:
                self.wfile.write(f"id: {event['seq']}\n".encode())
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()

        def do_POST(self) -> None:
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
            if not isinstance(body, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "expected an object"})
                return
            if path == "/send":
                web.submit(str(body.get("text") or ""))
                self._json(HTTPStatus.OK, {"ok": True, "queued": web.pending.qsize()})
            elif path == "/apply":
                paths = body.get("files")
                if not isinstance(paths, list) or not all(isinstance(name, str) for name in paths):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "files must be a list of paths"})
                    return
                web.submit(shlex.join(["/apply", "--", *paths]))
                self._json(HTTPStatus.OK, {"ok": True})
            elif path == "/stop":
                self._json(HTTPStatus.OK, {"ok": web.stop()})
            elif path == "/quit":
                web.close()
                self._json(HTTPStatus.OK, {"ok": True})

                def shutdown() -> None:
                    web.worker.join()
                    self.server.shutdown()

                threading.Thread(target=shutdown, daemon=True).start()
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    return Handler


def serve(
    web: WebSession, *, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True, page: str | None = None
) -> None:
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
        web.worker.join()
        server.server_close()


ASSETS = files("prax_bot.ui") / "assets"
PAGE = (ASSETS / "index.html").read_text(encoding="utf-8")
