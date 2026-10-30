"""One persistent main session and independently running, named work sessions."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import RLock
from typing import Any

from pydantic import BaseModel, Field
from rich.console import Console

from prax_bot.extensions.plugins import ExtraTool
from prax_bot.runtime.runs import Run, load_run, run_status, runs_dir
from prax_bot.runtime.session import Session
from prax_bot.runtime.sessions import SessionRegistry
from prax_bot.settings import Settings
from prax_bot.ui.web import WebSession
from prax_bot.workspace.types import EventFn


class NoArguments(BaseModel):
    pass


class StartSession(BaseModel):
    name: str = Field(description="A short name for this work session")
    task: str = Field(description="The instruction to run in its own workspace")


class ReadSession(BaseModel):
    session_id: str
    wait_seconds: float = Field(default=0, ge=0, le=30, description="Wait up to this long for current work to stop")


class SendSession(BaseModel):
    session_id: str
    task: str = Field(description="An ordinary instruction, not a slash command")


class StopSession(BaseModel):
    session_id: str


SessionFactory = Callable[[Console, EventFn, Run | None, Sequence[ExtraTool]], Session]


class WebSessions:
    """Own workers for this repository. Each browser chooses its own view, never a global active session."""

    def __init__(self, settings: Settings, root: Path, backend: str, make_session: SessionFactory) -> None:
        self.settings, self.root, self.backend = settings, root.resolve(), backend
        self.make_session = make_session
        self.registry = SessionRegistry(settings, self.root, backend)
        self.main_id: str = self.registry.main_id
        self._workers: dict[str, WebSession] = {}
        self._lock = RLock()
        self.closing = False
        try:
            self.get(self.main_id)
        except BaseException:
            self.registry.close()
            raise

    def _run(self, run_id: str) -> Run:
        if not run_id or run_id in (".", "..") or any(char in run_id for char in "/\\\0"):
            raise ValueError("invalid run ID")
        run = load_run(run_id, self.settings)
        if (
            Path(run.info.get("repo") or ".").resolve() != self.root
            or run.info.get("backend", "contree") != self.backend
        ):
            raise ValueError("the run belongs to a different repository or backend")
        return run

    def _replay(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        events = []
        for run_id in entry["run_ids"]:
            segment = self._run(run_id).events
            has_web_turns = any(event.get("kind") == "web.instruction.start" for event in segment)
            for event in segment:
                kind = event.get("kind")
                if kind == "instruction" and has_web_turns:
                    continue  # The saved web turn includes slash commands and the user's original text.
                shown_kind = "instruction.start" if kind == "instruction" else kind
                if isinstance(kind, str) and kind.startswith("web.instruction."):
                    shown_kind = kind.removeprefix("web.")
                # State is reconstructed from the final workspace, not old snapshots or UI cursors.
                if kind != "state":
                    events.append(
                        {
                            **event,
                            "kind": shown_kind,
                            "source_run": run_id,
                            "historical": True,
                        }
                    )
        return events

    def get(self, session_id: str | None = None) -> WebSession:
        session_id = session_id or self.main_id
        with self._lock:
            if self.closing:
                raise RuntimeError("the session manager is shutting down")
            entry = self.registry.get(session_id)
            worker = self._workers.get(session_id)
            if worker is not None:
                return worker
            past = self._run(entry["last_run_id"]) if entry["last_run_id"] else None
            if past is not None:
                if run_status(past).live:
                    raise RuntimeError("that run is still open in another process")
                if not past.checkpoints:
                    raise ValueError("that run has no checkpoint to reopen")

            def create(out: Console, hook: EventFn) -> Session:
                tools = self._main_tools() if entry["is_main"] else []
                session = self.make_session(out, hook, past, tools)
                try:
                    self.registry.attach_run(session_id, session.run_id)
                    if past is None:
                        session.diff()
                except BaseException:
                    session.close()
                    raise
                return session

            worker = WebSession(
                self.settings,
                self.root,
                self.backend,
                create,
                metadata={
                    "managed": True,
                    "session_id": session_id,
                    "session_name": entry["name"],
                    "is_main": entry["is_main"],
                    "main_session_id": self.main_id,
                },
                replay=self._replay(entry),
                keep_open=entry["is_main"],
            )
            self._workers[session_id] = worker
            return worker

    def open(self, session_id: str) -> str:
        with self._lock:
            worker = self._workers.get(session_id)
            if worker is not None and worker.closed:
                worker.worker.join()
                del self._workers[session_id]
            self.get(session_id)
            return session_id

    def create(self, name: str, task: str = "") -> str:
        if task:
            self._instruction(task)
        with self._lock:
            if self.closing:
                raise RuntimeError("the session manager is shutting down")
            entry = self.registry.create(name)
            worker = self.get(entry["id"])
            if task:
                worker.submit(task)
            return entry["id"]

    def import_run(self, run_id: str, name: str = "") -> str:
        with self._lock:
            run = self._run(run_id)
            for entry in self.registry.entries():
                if run_id in entry["run_ids"]:
                    return self.open(entry["id"])
            if run_status(run).live:
                raise RuntimeError("that run is still open in another process")
            if not run.checkpoints:
                raise ValueError("that run has no checkpoint to reopen")
            entry = self.registry.create(name or str(run.info.get("task") or run_id)[:120], run_id=run_id)
            return self.open(entry["id"])

    def rename(self, session_id: str, name: str) -> None:
        with self._lock:
            entry = self.registry.rename(session_id, name)
            if session_id in self._workers:
                worker = self._workers[session_id]
                worker.metadata["session_name"] = entry["name"]
                worker.emit(worker.state_event())

    def target(self, session_id: str | None, source_run: Any) -> WebSession:
        """Mutations target an already opened generation; an old tab cannot reopen and command a new one."""
        with self._lock:
            session_id = session_id or self.main_id
            self.registry.get(session_id)
            worker = self._workers.get(session_id)
            if (
                worker is None
                or worker.closed
                or worker.closing
                or worker.session is None
                or not isinstance(source_run, str)
                or source_run != worker.session.run_id
            ):
                raise RuntimeError("the session changed or is not ready; reopen it before sending work")
            return worker

    def close_session(self, session_id: str, source_run: Any) -> None:
        if session_id == self.main_id:
            raise RuntimeError("Main stays available while the server runs; use Stop to stop its work")
        self.target(session_id, source_run).close()

    def _entry_state(self, entry: dict[str, Any]) -> dict[str, Any]:
        worker = self._workers.get(entry["id"])
        if worker is not None:
            state = worker.state()
            return {
                **entry,
                **state,
                "id": entry["id"],
                "name": entry["name"],
                "task": worker.session.first_task or "" if worker.session else "",
                "files_changed": len(state.get("files", [])),
                "updated_at": max(entry["updated_at"], worker.history[-1]["t"] if worker.history else 0),
            }
        if entry["last_run_id"]:
            run = self._run(entry["last_run_id"])
            status = run_status(run)
            return {
                **entry,
                "status": "saved",
                "run_id": None,
                "task": status.task,
                "summary": run.info.get("summary", ""),
                "tokens": status.tokens,
                "verification": (run.info.get("verification") or {}).get("status", "not run"),
                "files_changed": status.files_changed,
                "error": None,
                "updated_at": status.last_activity.timestamp() if status.last_activity else entry["updated_at"],
            }
        return {
            **entry,
            "status": "saved",
            "run_id": None,
            "task": "",
            "summary": "",
            "tokens": 0,
            "verification": "not run",
            "files_changed": 0,
            "error": None,
        }

    def listing(self) -> dict[str, Any]:
        with self._lock:
            entries = self.registry.entries()
            sessions = []
            for entry in entries:
                try:
                    state = self._entry_state(entry)
                except (OSError, ValueError) as error:
                    state = {**entry, "status": "failed", "error": str(error)}
                sessions.append(
                    {
                        key: state.get(key)
                        for key in (
                            "id",
                            "name",
                            "is_main",
                            "run_id",
                            "last_run_id",
                            "run_ids",
                            "status",
                            "summary",
                            "task",
                            "updated_at",
                            "verification",
                            "files_changed",
                            "tokens",
                            "error",
                        )
                    }
                )
            attached = {run_id for entry in entries for run_id in entry["run_ids"]}
            saved = []
            base = runs_dir(self.settings)
            for directory in sorted(base.iterdir(), reverse=True) if base.exists() else []:
                if directory.name in attached or not (directory / "run.json").is_file():
                    continue
                try:
                    info = json.loads((directory / "run.json").read_text(encoding="utf-8"))
                    if (
                        info.get("repo") != str(self.root)
                        or info.get("backend", "contree") != self.backend
                        or info.get("kind") == "comparison-candidate"
                    ):
                        continue
                    run = Run(directory.name, directory, info)
                    status = run_status(run)
                    saved.append(
                        {
                            "run_id": run.run_id,
                            "task": status.task,
                            "state": status.state,
                            "summary": info.get("summary", ""),
                            "tokens": status.tokens,
                            "verification": (info.get("verification") or {}).get("status", "not run"),
                            "updated_at": status.last_activity.timestamp() if status.last_activity else 0,
                            "files_changed": status.files_changed,
                            "live": status.live,
                        }
                    )
                except (OSError, ValueError, TypeError):
                    continue  # One incomplete old run must not hide the rest of the saved history.
            return {
                "main_session_id": self.main_id,
                "sessions": sessions,
                "saved_runs": saved,
                "shutdown": self.closing,
            }

    @staticmethod
    def _instruction(task: str) -> str:
        if not task.strip() or task.lstrip().startswith("/"):
            raise ValueError("send an ordinary task instruction, not a slash command")
        return task.strip()

    def _work_session(self, session_id: str) -> WebSession:
        if session_id == self.main_id:
            raise ValueError("use these tools for work sessions; do not send work back to Main")
        self.open(session_id)
        return self.get(session_id)

    def _main_tools(self) -> list[ExtraTool]:
        def start(ws: Any, args: StartSession) -> str:
            return json.dumps({"session_id": self.create(args.name, self._instruction(args.task)), "status": "queued"})

        def list_sessions(ws: Any, args: NoArguments) -> str:
            listing = self.listing()
            return json.dumps({"main_session_id": self.main_id, "sessions": listing["sessions"]})

        def read(ws: Any, args: ReadSession) -> str:
            entry = self.registry.get(args.session_id)
            worker = self._workers.get(args.session_id)
            if args.session_id == self.main_id and args.wait_seconds:
                raise ValueError("Main cannot wait for itself")
            if worker is not None and args.wait_seconds:
                worker.idle.wait(args.wait_seconds)
            state = self._entry_state(entry)
            if worker is None and entry["last_run_id"]:
                run = self._run(entry["last_run_id"])
                state.update(diff=run.diff, checks=(run.info.get("verification") or {}).get("checks", []))
            state["diff"] = str(state.get("diff") or "")[:30000]
            return json.dumps(state)

        def send(ws: Any, args: SendSession) -> str:
            task = self._instruction(args.task)
            worker = self._work_session(args.session_id)
            worker.submit(task, source_run=worker.session.run_id if worker.session else None)
            return json.dumps({"session_id": args.session_id, "status": "queued"})

        def stop(ws: Any, args: StopSession) -> str:
            self.registry.get(args.session_id)
            if args.session_id == self.main_id:
                raise ValueError("use the user's Stop control for Main")
            worker = self._workers.get(args.session_id)
            return json.dumps({"session_id": args.session_id, "stop_requested": worker.stop() if worker else False})

        tools = [
            (
                "session_list",
                "List work sessions and their status. Main coordinates them; their work continues independently.",
                NoArguments,
                list_sessions,
            ),
            (
                "session_start",
                "Start a named work session on the current host repository in its own workspace. Its patch remains pending for user review; this does not apply changes. Return the session ID to the user.",
                StartSession,
                start,
            ),
            (
                "session_read",
                "Inspect a session's progress, summary, diff and checks. Optionally wait up to 30 seconds for its current work. Review evidence before claiming success.",
                ReadSession,
                read,
            ),
            (
                "session_send",
                "Send a follow-up task to a work session. Instructions queue on that session. Slash commands and applying patches are not supported.",
                SendSession,
                send,
            ),
            (
                "session_stop",
                "Request that a work session stop after its current call. Completed edits remain saved.",
                StopSession,
                stop,
            ),
        ]
        return [
            ExtraTool(name, description, model, handler, source="main-session")
            for name, description, model, handler in tools
        ]

    def close(self) -> None:
        with self._lock:
            self.closing = True
            for worker in self._workers.values():
                worker.close()

    def join(self) -> None:
        for worker in list(self._workers.values()):
            worker.worker.join()
        self.registry.close()
