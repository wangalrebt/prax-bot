"""A local web interface on the session: one page, events streamed to it, instructions posted back.

The server binds to localhost only. Every event the agent emits is forwarded to the page as JSON;
the page renders instructions, tool calls, results, summaries and diffs, and offers the slash commands.

Every request must name this server in its Host header, which a page that rebinds its own DNS name to
127.0.0.1 cannot do. Everything but the two static assets also needs the session token, which the
printed URL carries; the page is the one place the token is handed out, so it needs the token too.
"""

from __future__ import annotations

import json
import queue
import secrets
import shlex
import threading
import time
import webbrowser
from collections.abc import Callable, Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlparse

from rich.console import Console
from rich.text import Text

from prax_bot.runtime.runs import json_safe
from prax_bot.settings import Settings
from prax_bot.ui.session import handle_line

if TYPE_CHECKING:
    from prax_bot.ui.web_sessions import WebSessions

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
    "comparison": None,
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
        self,
        settings: Settings,
        root: Path,
        backend: str,
        make_session: Callable[[Console, Callable[..., None]], Any],
        *,
        metadata: dict[str, Any] | None = None,
        replay: Sequence[dict[str, Any]] = (),
        keep_open: bool = False,
    ) -> None:
        self.settings, self.root, self.backend = settings, root, backend
        self.make_session = make_session
        self.session: Any = None
        self.out = Console(file=cast(IO[str], LineSink(self.emit)), force_terminal=False, width=110)
        self.history: list[dict[str, Any]] = []
        self.clients: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.pending: queue.Queue[tuple[str, str | None] | None] = queue.Queue()
        self._work = threading.Condition()
        self._cancelled: list[tuple[str, str | None]] = []
        self.metadata = dict(metadata or {})
        self.keep_open = keep_open
        self.idle = threading.Event()
        self.busy = False
        self.closed = False
        self.closing = False
        self.error: str | None = None
        self.current_step = ""
        self.seq = 0
        for event in replay:
            self.emit(event)
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
        if kind.startswith("web.instruction."):
            return  # The worker persists web turns; their live UI events are emitted separately.
        safe = {k: json_safe(v) for k, v in payload.items()}
        if kind == "tool.call":
            self.current_step = f"step {payload.get('step')} · {payload.get('name')}"
        self.emit({"kind": kind, **safe})
        if kind in (
            "tool.call",
            "tool.result",
            "finish",
            "sandbox.ready",
            "goal.round",
            "comparison.start",
            "comparison.updated",
            "comparison.done",
            "comparison.selected",
        ) or (
            kind == "comparison.event"
            and payload.get("event_kind") in ("tool.call", "tool.result", "finish", "verification.result")
        ):
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
            **self.metadata,
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
    def submit(self, line: str, *, source_run: str | None = None) -> None:
        line = line.strip()
        with self._work:
            if self.closing or self.closed:
                raise RuntimeError("the session is closing or closed; reopen it before sending work")
            if line and line.lower() != "/stop":
                self.idle.clear()
                self.pending.put((line, source_run))
                self._work.notify()
        if line.lower() == "/stop":
            self.stop()
        elif line:
            self.emit({"kind": "queued", "text": line, "queued": self.pending.qsize()})

    def stop(self) -> bool:
        cancelled: list[tuple[str, str | None]] = []
        with self._work:
            closing_sentinel = False
            while True:
                try:
                    queued = self.pending.get_nowait()
                except queue.Empty:
                    break
                if queued is None:
                    closing_sentinel = True
                else:
                    cancelled.append(queued)
            if closing_sentinel:
                self.pending.put(None)
            self._cancelled.extend(cancelled)
            running = self.busy
            if running and self.session is not None:
                self.session.request_stop()
            if not running and self.session is not None:
                self.idle.set()
            self._work.notify()
        for line, source_run in cancelled:
            self.emit({"kind": "instruction.cancelled", "text": line, "source_run": source_run})
        if running or cancelled:
            self.emit(self.state_event())
        return bool(running or cancelled)

    def _persist_cancelled(self, cancelled: Sequence[tuple[str, str | None]]) -> None:
        """Only the worker writes cancellations, so shutdown cannot close their log mid-write."""
        if self.session is not None and hasattr(self.session, "on_event"):
            for line, source_run in cancelled:
                self.session.on_event("web.instruction.cancelled", text=line, source_run=source_run)

    def _worker(self) -> None:
        try:
            self.session = self.make_session(self.out, self.hook)
            self.emit(self.state_event())
            while True:
                with self._work:
                    while not self.closing and self.pending.empty() and not self._cancelled:
                        self.idle.set()
                        self._work.wait()
                    cancelled, self._cancelled = self._cancelled, []
                    queued = self.pending.get_nowait() if not self.pending.empty() else None
                    closing = self.closing
                    if not closing and queued is not None:
                        line, source_run = queued
                        self.busy = source_run is None or source_run == self.session.run_id
                        self.idle.clear()
                self._persist_cancelled(cancelled)
                if closing:
                    break
                if queued is None:
                    continue
                line, source_run = queued
                if source_run is not None and source_run != self.session.run_id:
                    self.emit({"kind": "log", "text": "✗ Session changed; that queued instruction was not run."})
                    continue
                if hasattr(self.session, "on_event"):
                    self.session.on_event("web.instruction.start", text=line)
                self.emit({"kind": "instruction.start", "text": line})
                self.emit(self.state_event())
                keep_going = True
                try:
                    keep_going = handle_line(self.session, line, self.out)
                except Exception as error:
                    self.emit({"kind": "log", "text": f"✗ {type(error).__name__}: {str(error)[:500]}"})
                finally:
                    with self._work:
                        self.busy = False
                        # A stop can arrive after runtime cleanup but before this command is settled.
                        cancel = getattr(self.session, "cancel", None)
                        if cancel is not None:
                            cancel.clear()
                if hasattr(self.session, "on_event"):
                    self.session.on_event("web.instruction.done", text=line)
                self.emit({"kind": "instruction.done", "text": line})
                self.emit(self.state_event())
                if not keep_going:
                    if self.keep_open:
                        self.emit(
                            {
                                "kind": "log",
                                "text": "Main stays available while Prax is running. Use Stop to stop its current work.",
                            }
                        )
                    else:
                        self.close()
        except Exception as error:
            self.error = f"{type(error).__name__}: {str(error)[:500]}"
            self.emit({"kind": "log", "text": f"could not start: {self.error}"})
        finally:
            if self.session is not None:
                try:
                    with self._work:
                        cancelled, self._cancelled = self._cancelled, []
                    self._persist_cancelled(cancelled)
                    self.session.close()
                except Exception as error:
                    self.emit({"kind": "log", "text": f"✗ while closing: {str(error)[:500]}"})
            with self._work:
                self.closed = True
                self.idle.set()
            self.emit(self.state_event())

    def close(self) -> None:
        with self._work:
            if self.closing or self.closed:
                return
            self.closing = True
            self.stop()
            self.pending.put(None)
            self._work.notify()
        self.emit(self.state_event())


def make_handler(web: WebSession | WebSessions, token: str, page: str) -> type[BaseHTTPRequestHandler]:
    from prax_bot.ui.web_sessions import WebSessions

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _authorized(self) -> bool:
            offered = (self.headers.get("X-Prax-Token"), parse_qs(urlparse(self.path).query).get("token", [""])[0])
            return any(secrets.compare_digest(value.encode(), token.encode()) for value in offered if value)

        def _host_allowed(self) -> bool:
            """The Host header names this server: its bound address, 127.0.0.1 or localhost, on its own port."""
            bound, port = cast(tuple[str, int], self.server.server_address)  # an IPv4 host and port
            host = (self.headers.get("Host") or "").strip().lower()
            name, _, given = host.rpartition(":") if ":" in host else (host, "", "80")
            return name in {str(bound), "127.0.0.1", "localhost"} and given == str(port)

        def _refused(self, *, needs_token: bool) -> bool:
            """Answer 403 and return True when the request may not go on."""
            if not self._host_allowed():
                self._json(HTTPStatus.FORBIDDEN, {"error": "this server only answers requests addressed to it"})
                return True
            if needs_token and not self._authorized():
                self._json(HTTPStatus.FORBIDDEN, {"error": "bad token"})
                return True
            return False

        def _send(self, status: int, data: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status: int, body: Any) -> None:
            self._send(status, json.dumps(body).encode("utf-8"), "application/json")

        def _target(self, body: dict[str, Any] | None = None) -> WebSession:
            if isinstance(web, WebSessions):
                session_id = parse_qs(urlparse(self.path).query).get("session", [None])[0]
                return web.get(session_id) if body is None else web.target(session_id, body.get("source_run"))
            return web

        def _submit(self, target: WebSession, text: str, body: dict[str, Any]) -> None:
            if isinstance(web, WebSessions):
                target.submit(text, source_run=body["source_run"])
            else:
                target.submit(text)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if self._refused(needs_token=path not in ("/assets/app.css", "/assets/app.js")):
                return
            if path == "/":
                self._send(
                    HTTPStatus.OK,
                    page.replace("__TOKEN__", token).encode("utf-8"),
                    "text/html; charset=utf-8",
                    {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},  # the page carries the token
                )
            elif path in ("/assets/app.css", "/assets/app.js"):
                name = path.rsplit("/", 1)[-1]
                content_type = "text/css" if name.endswith(".css") else "text/javascript"
                self._send(HTTPStatus.OK, (ASSETS / name).read_bytes(), content_type)
            elif path in ("/state", "/events", "/sessions"):
                try:
                    if path == "/sessions" and isinstance(web, WebSessions):
                        self._json(HTTPStatus.OK, web.listing())
                        return
                    target = self._target()
                except (ValueError, OSError, RuntimeError) as error:
                    self._json(HTTPStatus.CONFLICT, {"error": str(error)})
                    return
                if path == "/state":
                    self._json(HTTPStatus.OK, target.state())
                    return
                if path == "/sessions":
                    self._json(HTTPStatus.OK, {"sessions": [], "saved_runs": []})
                    return
                self._events(target)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _events(self, target: WebSession) -> None:
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
            history, q = target.subscribe(after)
            try:
                for event in history:
                    self._send_event(event)
                self._send_event(target.state_event())
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
                target.unsubscribe(q)

        def _send_event(self, event: dict[str, Any]) -> None:
            if "seq" in event:
                self.wfile.write(f"id: {event['seq']}\n".encode())
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()

        def do_POST(self) -> None:
            if self._refused(needs_token=True):
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
            try:
                self._post(path, body)
            except (ValueError, TypeError, FileNotFoundError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except RuntimeError as error:
                self._json(HTTPStatus.CONFLICT, {"error": str(error)})

        def _post(self, path: str, body: dict[str, Any]) -> None:
            if isinstance(web, WebSessions) and path.startswith("/sessions/"):
                session_id = body.get("session_id", "")
                if not isinstance(session_id, str) or (
                    path in ("/sessions/open", "/sessions/rename", "/sessions/close") and not session_id
                ):
                    raise ValueError("session_id is required")
                if path == "/sessions/new":
                    task = body.get("task", "")
                    if not isinstance(task, str):
                        raise ValueError("task must be a string")
                    session_id = web.create(body.get("name", ""), task)
                elif path == "/sessions/open":
                    session_id = web.open(session_id)
                elif path == "/sessions/import":
                    if not isinstance(body.get("run_id"), str) or not isinstance(body.get("name", ""), str):
                        raise ValueError("run_id and name must be strings")
                    session_id = web.import_run(body["run_id"], body.get("name", ""))
                elif path == "/sessions/rename":
                    web.rename(session_id, body.get("name", ""))
                elif path == "/sessions/close":
                    web.close_session(session_id, body.get("source_run"))
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                self._json(HTTPStatus.OK, {"ok": True, "session_id": session_id})
                return
            if path == "/shutdown" and isinstance(web, WebSessions):
                self._shutdown()
                return
            target = self._target(body)
            if path == "/send":
                self._submit(target, str(body.get("text") or ""), body)
                self._json(HTTPStatus.OK, {"ok": True, "queued": target.pending.qsize()})
            elif path == "/apply":
                paths = body.get("files")
                if not isinstance(paths, list) or not all(isinstance(name, str) for name in paths):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "files must be a list of paths"})
                    return
                self._submit(target, shlex.join(["/apply", "--", *paths]), body)
                self._json(HTTPStatus.OK, {"ok": True})
            elif path == "/compare":
                objective = body.get("objective")
                approach_a, approach_b = body.get("approach_a", ""), body.get("approach_b", "")
                if not isinstance(objective, str) or not objective.strip():
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "objective must be a nonempty string"})
                    return
                if not isinstance(approach_a, str) or not isinstance(approach_b, str):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "approaches must be strings"})
                    return
                self._submit(
                    target, shlex.join(["/compare", "--a", approach_a, "--b", approach_b, "--", objective]), body
                )
                self._json(HTTPStatus.OK, {"ok": True})
            elif path == "/choose":
                candidate, comparison_id = body.get("candidate"), body.get("comparison_id")
                if candidate not in ("A", "B"):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "candidate must be A or B"})
                    return
                if not isinstance(comparison_id, str) or not comparison_id.strip():
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "comparison_id must be a nonempty string"})
                    return
                self._submit(target, shlex.join(["/choose", candidate, "--comparison", comparison_id]), body)
                self._json(HTTPStatus.OK, {"ok": True})
            elif path == "/stop":
                self._json(HTTPStatus.OK, {"ok": target.stop()})
            elif path == "/quit":
                if isinstance(web, WebSessions):
                    web.close_session(target.metadata["session_id"], body.get("source_run"))
                    self._json(HTTPStatus.OK, {"ok": True})
                else:
                    self._shutdown()
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _shutdown(self) -> None:
            web.close()
            self._json(HTTPStatus.OK, {"ok": True})

            def shutdown() -> None:
                if isinstance(web, WebSessions):
                    web.join()
                else:
                    web.worker.join()
                self.server.shutdown()

            threading.Thread(target=shutdown, daemon=True).start()

    return Handler


def serve(
    web: WebSession | WebSessions,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    page: str | None = None,
) -> None:
    """Serve until shutdown or an interrupt. Port 0 picks a free port."""
    from prax_bot.ui.web_sessions import WebSessions

    token = secrets.token_urlsafe(16)
    try:
        server = ThreadingHTTPServer((host, port), make_handler(web, token, page or PAGE))
    except BaseException:
        web.close()
        if isinstance(web, WebSessions):
            web.join()
        else:
            web.worker.join()
        raise
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
        if isinstance(web, WebSessions):
            web.join()
        else:
            web.worker.join()
        server.server_close()


ASSETS = files("prax_bot.ui") / "assets"
PAGE = (ASSETS / "index.html").read_text(encoding="utf-8")
