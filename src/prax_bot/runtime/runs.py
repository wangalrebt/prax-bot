"""Run records, event logs, checkpoint recovery, and status data."""

from __future__ import annotations

import dataclasses
import fcntl
import json
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, TextIO

from prax_bot.core.prompts import prompt
from prax_bot.runtime.apply import ApplyTransaction
from prax_bot.runtime.storage import write_json
from prax_bot.settings import Settings
from prax_bot.workspace.repository import DIFF_OUTPUT_LIMIT
from prax_bot.workspace.types import EventFn, Workspace


@dataclass
class Run:
    run_id: str
    directory: Path
    info: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def diff(self) -> str:
        p = self.directory / "diff.patch"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    @property
    def checkpoints(self) -> list[dict[str, Any]]:
        return list(self.info.get("checkpoints") or [])

    @property
    def pending_apply(self) -> ApplyTransaction | None:
        return ApplyTransaction.load(self.directory, self.info)

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        """Tool calls paired with the first line of their result, in order."""
        calls: list[dict[str, Any]] = []
        for e in self.events:
            if e.get("kind") == "tool.call":
                calls.append({"step": e.get("step"), "name": e.get("name"), "args": e.get("args", ""), "result": ""})
            elif e.get("kind") == "tool.result" and calls:
                out = str(e.get("output") or "")
                calls[-1]["result"] = out.splitlines()[0] if out else ""
                calls[-1]["error"] = e.get("error", out.startswith("error:"))  # fallback for older event logs
        return calls


def runs_dir(settings: Settings | None = None) -> Path:
    return (settings or Settings.from_env()).home / "runs"


def list_runs(settings: Settings | None = None, limit: int = 10) -> list[Run]:
    base = runs_dir(settings)
    if not base.exists():
        return []
    dirs = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)
    return [load_run(d.name, settings) for d in dirs[:limit]]


def load_run(run_id: str, settings: Settings | None = None) -> Run:
    directory = runs_dir(settings) / run_id
    if not directory.is_dir():
        raise FileNotFoundError(f"no run {run_id} under {runs_dir(settings)}")
    info_path = directory / "run.json"
    info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.exists() else {"run_id": run_id}
    events = []
    ev = directory / "events.jsonl"
    if ev.exists():
        for line in ev.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return Run(run_id=run_id, directory=directory, info=info, events=events)


def latest_run_id(settings: Settings | None = None) -> str | None:
    runs = list_runs(settings, limit=1)
    return runs[0].run_id if runs else None


def checkpoint_diff(run: Run, step: int, settings: Settings | None = None) -> str:
    """The diff of a checkpoint against the run's baseline, recomputed from the stored state."""
    checkpoints = run.checkpoints
    if not 0 <= step < len(checkpoints):
        raise ValueError(f"run {run.run_id} has checkpoints 0 to {len(checkpoints) - 1}")
    image = checkpoints[step]["image"]
    command = "git add -A && git diff --cached --binary HEAD"
    if image.startswith("local:"):
        settings = settings or Settings.from_env()
        root = Path(run.info.get("workspace_root") or settings.home / "local" / run.run_id)
        snap = root / "snapshots" / image.split(":", 1)[1]
        if not snap.is_dir():
            raise FileNotFoundError(f"local snapshot {snap} no longer exists")
        r = subprocess.run(["bash", "-c", command], cwd=snap, capture_output=True, text=True, check=False)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
        return r.stdout
    from prax_bot.settings import WORKDIR
    from prax_bot.workspace.sandbox import make_client

    result = (
        make_client()
        .images.use(image)
        .run(shell=command, cwd=WORKDIR, disposable=True, timeout=120, truncate_output_at=DIFF_OUTPUT_LIMIT)
        .wait()
    )
    if result.exit_code != 0:
        raise RuntimeError(str(result.stderr or "").strip())
    return str(result.stdout or "")


MAX_CONTEXT_DIFF_CHARS = 30_000


def continuation_task(original_task: str, summary: str | None, diff: str, instruction: str) -> str:
    """The task text for continuing earlier work: what was asked, what was done, the diff so far, what to do now."""
    shown = diff.strip()
    if len(shown) > MAX_CONTEXT_DIFF_CHARS:
        shown = shown[:MAX_CONTEXT_DIFF_CHARS] + "\n... (diff truncated; run `git diff HEAD` to see all of it)"
    return prompt(
        "continuation",
        original_task=original_task.strip(),
        summary=(summary or "").strip() or "(none)",
        diff=shown or "(no changes yet)",
        instruction=instruction.strip(),
    )


def reopen_workspace(
    run: Run, step: int, settings: Settings, on_event: EventFn | None = None, new_run_id: str = "resume"
) -> Workspace:
    """A live workspace rebuilt from one checkpoint of a past run.

    Sandboxes are immutable images, so the contree backend simply starts from that image.
    The local backend clones the snapshot directory and the run's virtualenv into a new run root.
    """
    checkpoints = run.checkpoints
    if not 0 <= step < len(checkpoints):
        raise ValueError(f"run {run.run_id} has checkpoints 0 to {len(checkpoints) - 1}")
    pending = run.pending_apply
    if pending is not None and step != len(checkpoints) - 1:
        raise ValueError("resume the last checkpoint to finish recording this run's applied patch first")
    image = checkpoints[step]["image"]
    if image.startswith("local:"):
        from prax_bot.workspace.local import LocalWorkspace, clone_dir

        old_root = Path(run.info.get("workspace_root") or settings.home / "local" / run.run_id)
        snap = old_root / "snapshots" / image.split(":", 1)[1]
        if not snap.is_dir():
            raise FileNotFoundError(f"local snapshot {snap} no longer exists")
        root = settings.home / "local" / new_run_id
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        clone_dir(snap, root / "current")
        if (old_root / "venv").is_dir():
            clone_dir(old_root / "venv", root / "venv")
        workspace = LocalWorkspace(root, command_timeout=settings.command_timeout, on_event=on_event)
    else:
        from prax_bot.workspace.sandbox import SandboxWorkspace, make_client

        client = make_client()
        workspace = SandboxWorkspace(
            client, client.images.use(image), command_timeout=settings.command_timeout, on_event=on_event
        )
    if pending is not None:
        pending.finish(Path(run.info["repo"]), workspace)
    return workspace


@dataclass
class RunStatus:
    run_id: str
    kind: str  # run, resume, goal, session
    task: str
    state: str  # finished, stopped: <reason>, in progress, idle, inactive, unknown
    started: datetime | None
    last_activity: datetime | None
    steps: int | None
    tokens: int | None
    cost: float | None
    files_changed: int
    applied: bool | None
    last_recorded_state: str | None = None
    live: bool | None = None
    last_tool: str | None = None
    goal: dict[str, Any] | None = None


def run_started(run_id: str) -> datetime | None:
    try:
        return datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def run_status(run: Run) -> RunStatus:
    """What one run is, what it did, and whether it is still going."""
    info = run.info
    events_path = run.directory / "events.jsonl"
    last_activity = None
    if events_path.exists():
        last_activity = datetime.fromtimestamp(events_path.stat().st_mtime, tz=UTC)
    live = None
    if events_path.exists():
        with events_path.open() as log:
            try:
                fcntl.flock(log, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                live = True
            else:
                fcntl.flock(log, fcntl.LOCK_UN)
                if info.get("activity_tracking") == "event_lock":
                    live = False
    kind = info.get("kind") or (
        "session"
        if info.get("session")
        else "goal"
        if info.get("goal")
        else "resume"
        if info.get("resumed_from")
        else "run"
    )
    calls = [e for e in run.events if e.get("kind") == "tool.call"]
    last_tool = f"{calls[-1].get('name')} (step {calls[-1].get('step')})" if calls else None
    recorded_state = info.get("state")
    if live:
        state = "in progress" if recorded_state == "working" else "idle" if recorded_state == "idle" else "starting"
    elif recorded_state in ("working", "idle"):
        state = "inactive" if live is False else "unknown"
    elif "finished" in info:
        state = "finished" if info.get("finished") else f"stopped: {info.get('reason', '')}"
    else:
        state = "unknown"
    diff = run.diff
    tokens = None
    if "prompt_tokens" in info:
        tokens = int(info.get("prompt_tokens") or 0) + int(info.get("completion_tokens") or 0)
    return RunStatus(
        run_id=run.run_id,
        kind=kind,
        task=str(info.get("task") or "").strip(),
        state=state,
        started=run_started(run.run_id),
        last_activity=last_activity,
        steps=info.get("steps"),
        tokens=tokens,
        cost=info.get("sandbox_cost"),
        files_changed=diff.count("diff --git "),
        applied=info.get("applied"),
        last_recorded_state=recorded_state,
        live=live,
        last_tool=last_tool,
        goal=info.get("goal"),
    )


def local_workspaces(settings: Settings) -> tuple[int, int]:
    """Count and size in bytes of the local backend's workspaces, which can be large."""
    base = settings.home / "local"
    if not base.is_dir():
        return 0, 0
    dirs = [d for d in base.iterdir() if d.is_dir()]
    r = subprocess.run(["du", "-sk", *map(str, dirs)], capture_output=True, text=True, check=False) if dirs else None
    size = 0
    if r is not None and r.returncode == 0:
        size = sum(int(line.split()[0]) * 1024 for line in r.stdout.splitlines() if line.split())
    return len(dirs), size


def status_summary(settings: Settings, limit: int = 10) -> dict[str, Any]:
    base = runs_dir(settings)
    total = sum(1 for d in base.iterdir() if d.is_dir()) if base.exists() else 0
    statuses = [run_status(r) for r in list_runs(settings, limit=limit)]
    count, size = local_workspaces(settings)
    return {"runs": statuses, "total_runs": total, "local_workspaces": count, "local_bytes": size}


def ago(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return "?"
    seconds = max(0, int(((now or datetime.now(UTC)) - when).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def json_safe(value: Any) -> Any:
    """A JSON-serialisable view of an event payload: dataclasses, containers, plain objects, or their repr."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if hasattr(value, "__dict__"):
        return {k: json_safe(v) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def build_record(
    *,
    run_id: str,
    task: str,
    followups: list[str],
    repo: Path,
    settings: Settings,
    llm: Any,
    ws: Workspace,
    backend: str,
    base_image: str | None,
    result: Any,
    steps: int,
    summary: str,
    elapsed_s: float,
    resumed_from: dict[str, Any] | None = None,
    diff_error: str | None = None,
    applied: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The run.json record: what was asked, what the model and sandbox did, and where it ended up.

    One shape for every front end, so `praxbot status`, `inspect` and `report` read all runs the same way.
    """
    return {
        "run_id": run_id,
        "task": task,
        "followups": list(followups),
        "resumed_from": resumed_from,
        "repo": str(repo),
        "model": settings.model,
        "temperature": settings.temperature,
        "top_p": settings.top_p,
        "effort": settings.effort,
        "investigate_model": settings.investigate_model,
        "usage_by_model": llm.usage.by_model,
        "backend": backend,
        "base_image": base_image,
        "finished": bool(result and result.finished),
        "reason": result.reason if result else "session",
        "steps": steps,
        "summary": summary,
        "model_calls": llm.usage.calls,
        "prompt_tokens": llm.usage.prompt_tokens,
        "completion_tokens": llm.usage.completion_tokens,
        "sandbox_commands": ws.commands,
        "sandbox_cost": ws.total_cost,
        "baseline_image": ws.checkpoints[0].image,
        "final_image": ws.image_id,
        "checkpoints": [dataclasses.asdict(c) for c in ws.checkpoints],
        "elapsed_s": elapsed_s,
        "diff_error": diff_error,
        "applied": applied,
        **(extra or {}),
    }


def new_run(settings: Settings, on_event: EventFn | None = None) -> tuple[str, Path, TextIO, EventFn]:
    """Create an event log; observers receive the same events after they are persisted."""
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    directory = settings.home / "runs" / run_id
    directory.mkdir(parents=True)
    events_file = (directory / "events.jsonl").open("a", encoding="utf-8")
    try:
        fcntl.flock(events_file, fcntl.LOCK_EX)
    except BaseException:
        events_file.close()
        raise

    lock = Lock()

    def emit(kind: str, **payload: Any) -> None:
        record = {"t": time.time(), "kind": kind, **{k: json_safe(v) for k, v in payload.items()}}
        with lock:
            events_file.write(json.dumps(record) + "\n")
            events_file.flush()
        if on_event is not None:
            on_event(kind, **payload)

    return run_id, directory, events_file, emit


def write_record(run_dir: Path, record: dict[str, Any]) -> None:
    """Replace the record atomically so status readers never see half-written JSON."""
    write_json(run_dir / "run.json", record)
