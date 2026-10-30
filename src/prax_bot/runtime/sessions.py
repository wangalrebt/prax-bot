"""Persistent named sessions and their run lineage for one repository and backend."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Any

from prax_bot.runtime.storage import write_json
from prax_bot.settings import Settings


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120:
        raise ValueError("session name must contain 1 to 120 characters")
    return value.strip()


def _run_id(value: Any) -> str:
    if not isinstance(value, str) or not value or value in (".", "..") or any(c in value for c in "/\\\0"):
        raise ValueError("run ID must be a single path component other than '.' or '..'")
    return value


class SessionRegistry:
    """Own the session manifest until close; opening another owner fails immediately."""

    def __init__(self, settings: Settings, root: Path, backend: str) -> None:
        self._root = str(root.resolve())
        self._backend = backend
        identity = json.dumps([self._root, backend]).encode("utf-8")
        key = hashlib.sha256(identity).hexdigest()
        directory = settings.home / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        self._path = directory / f"{key}.json"
        self._lock = Lock()
        self._closed = False
        self._lock_file = (directory / f"{key}.lock").open("a", encoding="utf-8")
        try:
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(f"a session manager is already open for {self._root} ({backend})") from None
            if self._path.exists():
                try:
                    self.main_id, self._entries = self._load()
                except (ValueError, TypeError, KeyError) as exc:
                    raise ValueError(f"invalid session manifest {self._path}: {exc}") from exc
            else:
                main = self._new_entry("Main", is_main=True)
                self.main_id = main["id"]
                self._entries = {self.main_id: main}
                self._write(self._entries)
        except BaseException:
            self._lock_file.close()
            self._closed = True
            raise

    def _load(self) -> tuple[str, dict[str, dict[str, Any]]]:
        data = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("root") != self._root or data.get("backend") != self._backend:
            raise ValueError("repository or backend does not match")
        main_id = data.get("main_id")
        if not isinstance(main_id, str) or not main_id or not isinstance(data.get("sessions"), list):
            raise ValueError("main_id and sessions are required")
        entries: dict[str, dict[str, Any]] = {}
        for entry in data["sessions"]:
            if not isinstance(entry, dict):
                raise TypeError("session entry must be an object")
            session_id = entry.get("id")
            if not isinstance(session_id, str) or not session_id or session_id in entries:
                raise ValueError("session IDs must be nonempty and unique")
            _name(entry["name"])
            if type(entry.get("is_main")) is not bool or entry["is_main"] != (session_id == main_id):
                raise ValueError("main session identity is inconsistent")
            run_ids = entry.get("run_ids")
            if not isinstance(run_ids, list):
                raise TypeError("run_ids must be a list")
            for run_id in run_ids:
                _run_id(run_id)
            if len(run_ids) != len(set(run_ids)) or entry["last_run_id"] != (run_ids[-1] if run_ids else None):
                raise ValueError("run lineage is inconsistent")
            for field in ("created_at", "updated_at"):
                value = entry[field]
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError(f"{field} must be a finite timestamp")
                entry[field] = float(value)
            entries[session_id] = entry
        if main_id not in entries:
            raise ValueError("main session is missing")
        return main_id, entries

    @staticmethod
    def _new_entry(name: str, *, is_main: bool = False, run_id: str | None = None) -> dict[str, Any]:
        now = time.time()
        return {
            "id": uuid.uuid4().hex,
            "name": name,
            "is_main": is_main,
            "run_ids": [run_id] if run_id is not None else [],
            "last_run_id": run_id,
            "created_at": now,
            "updated_at": now,
        }

    def _write(self, entries: dict[str, dict[str, Any]]) -> None:
        write_json(
            self._path,
            {"root": self._root, "backend": self._backend, "main_id": self.main_id, "sessions": list(entries.values())},
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("session registry is closed")

    def _get(self, session_id: str) -> dict[str, Any]:
        try:
            return self._entries[session_id]
        except KeyError:
            raise ValueError(f"unknown session: {session_id}") from None

    def entries(self) -> list[dict[str, Any]]:
        """Return the explicit main session first, then work sessions by last activity."""
        with self._lock:
            self._ensure_open()
            entries = sorted(
                self._entries.values(),
                key=lambda entry: (entry["id"] != self.main_id, -entry["updated_at"], entry["id"]),
            )
            return copy.deepcopy(entries)

    def get(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            return copy.deepcopy(self._get(session_id))

    def create(self, name: str, *, run_id: str | None = None) -> dict[str, Any]:
        name = _name(name)
        if run_id is not None:
            _run_id(run_id)
        with self._lock:
            self._ensure_open()
            entry = self._new_entry(name, run_id=run_id)
            entries = {**self._entries, entry["id"]: entry}
            self._write(entries)
            self._entries = entries
            return copy.deepcopy(entry)

    def rename(self, session_id: str, name: str) -> dict[str, Any]:
        name = _name(name)
        with self._lock:
            self._ensure_open()
            entry = {**self._get(session_id), "name": name, "updated_at": time.time()}
            entries = {**self._entries, session_id: entry}
            self._write(entries)
            self._entries = entries
            return copy.deepcopy(entry)

    def attach_run(self, session_id: str, run_id: str) -> None:
        run_id = _run_id(run_id)
        with self._lock:
            self._ensure_open()
            old = self._get(session_id)
            if run_id in old["run_ids"]:
                return
            entry = {
                **old,
                "run_ids": [*old["run_ids"], run_id],
                "last_run_id": run_id,
                "updated_at": time.time(),
            }
            entries = {**self._entries, session_id: entry}
            self._write(entries)
            self._entries = entries

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._lock_file.close()
                self._closed = True
