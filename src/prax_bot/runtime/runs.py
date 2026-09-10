"""Past runs: listing, inspecting the checkpoint chain, diffs per checkpoint, and an HTML report."""

from __future__ import annotations

import html
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prax_bot.core.prompts import prompt
from prax_bot.settings import Settings
from prax_bot.workspace.sandbox import DIFF_OUTPUT_LIMIT


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
    def tool_calls(self) -> list[dict[str, Any]]:
        """Tool calls paired with the first line of their result, in order."""
        calls: list[dict[str, Any]] = []
        for e in self.events:
            if e.get("kind") == "tool.call":
                calls.append({"step": e.get("step"), "name": e.get("name"), "args": e.get("args", ""), "result": ""})
            elif e.get("kind") == "tool.result" and calls:
                out = str(e.get("output") or "")
                calls[-1]["result"] = out.splitlines()[0] if out else ""
                calls[-1]["error"] = out.startswith("error:")
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
        snap = settings.home / "local" / run.run_id / "snapshots" / image.split(":", 1)[1]
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


def reopen_workspace(run: Run, step: int, settings: Settings, on_event: Any = None, new_run_id: str = "resume") -> Any:
    """A live workspace rebuilt from one checkpoint of a past run.

    Sandboxes are immutable images, so the contree backend simply starts from that image.
    The local backend clones the snapshot directory and the run's virtualenv into a new run root.
    """
    checkpoints = run.checkpoints
    if not 0 <= step < len(checkpoints):
        raise ValueError(f"run {run.run_id} has checkpoints 0 to {len(checkpoints) - 1}")
    image = checkpoints[step]["image"]
    if image.startswith("local:"):
        from prax_bot.workspace.local import LocalWorkspace, clone_dir

        old_root = settings.home / "local" / run.run_id
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
        return LocalWorkspace(root, command_timeout=settings.command_timeout, on_event=on_event)
    from prax_bot.workspace.sandbox import Workspace, make_client

    client = make_client()
    return Workspace(client, client.images.use(image), command_timeout=settings.command_timeout, on_event=on_event)


IN_PROGRESS_WINDOW_S = 30 * 60  # a run without a record whose log moved within this window is still running


@dataclass
class RunStatus:
    run_id: str
    kind: str  # run, resume, goal, session
    task: str
    state: str  # finished, stopped: <reason>, in progress, abandoned
    started: datetime | None
    last_activity: datetime | None
    steps: int | None
    tokens: int | None
    cost: float | None
    files_changed: int
    applied: bool | None
    last_tool: str | None = None
    goal: dict[str, Any] | None = None


def run_started(run_id: str) -> datetime | None:
    try:
        return datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def run_status(run: Run, now: datetime | None = None) -> RunStatus:
    """What one run is, what it did, and whether it is still going."""
    now = now or datetime.now(UTC)
    info = run.info
    events_path = run.directory / "events.jsonl"
    last_activity = None
    if events_path.exists():
        last_activity = datetime.fromtimestamp(events_path.stat().st_mtime, tz=UTC)
    kind = (
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
    if "finished" in info:
        state = "finished" if info.get("finished") else f"stopped: {info.get('reason', '')}"
    elif last_activity and (now - last_activity).total_seconds() < IN_PROGRESS_WINDOW_S:
        state = "in progress"
    else:
        state = "abandoned"
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


def _short_args(name: str, raw: str) -> str:
    from prax_bot.runtime.render import describe_call

    return describe_call(name, raw)


def render_html(run: Run) -> str:
    """A self-contained HTML report for one run."""
    info = run.info
    e = html.escape
    usage = info.get("usage_by_model") or {}
    rows = "".join(
        f"<tr class='{'err' if c.get('error') else ''}'><td>{c['step']}</td><td>{e(str(c['name']))}</td>"
        f"<td>{e(_short_args(str(c['name']), str(c['args'])))}</td><td>{e(str(c['result'])[:160])}</td></tr>"
        for c in run.tool_calls
    )
    cps = "".join(
        f"<tr><td>{c['step']}</td><td><code>{e(str(c['image']))}</code></td><td>{e(str(c['label']))}</td></tr>"
        for c in run.checkpoints
    )
    by_model = "".join(
        f"<tr><td><code>{e(m)}</code></td><td>{u.get('calls', 0)}</td><td>{u.get('prompt_tokens', 0):,}</td><td>{u.get('completion_tokens', 0):,}</td></tr>"
        for m, u in usage.items()
    )
    status = "finished" if info.get("finished") else f"stopped: {info.get('reason', '')}"
    diff = run.diff
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Prax Bot run {e(run.run_id)}</title>
<style>
body{{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;padding:32px;max-width:1100px;color:#1a2321;background:#f6f8f7}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:28px 0 8px}} code,pre{{font:13px/1.45 ui-monospace,Menlo,monospace}}
table{{border-collapse:collapse;width:100%;font-size:14px}} td,th{{text-align:left;vertical-align:top;padding:5px 10px 5px 0;border-bottom:1px solid #d9dfdc}}
th{{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:#6b7772}} tr.err td{{color:#a33d2a}}
pre{{background:#17211e;color:#dfe6e2;padding:14px;overflow:auto;border-radius:4px}} .kv td:first-child{{color:#6b7772;width:140px}}
.summary{{background:#fff;border-left:3px solid #0f6a86;padding:10px 14px;white-space:pre-wrap}}
</style></head><body>
<h1>Prax Bot run {e(run.run_id)}</h1>
<div>{e(str(info.get("task", "")))}</div>
<h2>Run</h2>
<table class="kv">
<tr><td>result</td><td>{e(status)}</td></tr>
<tr><td>repository</td><td><code>{e(str(info.get("repo", "")))}</code></td></tr>
<tr><td>backend</td><td>{e(str(info.get("backend", "")))}{(" · image " + e(str(info.get("base_image")))) if info.get("base_image") else ""}</td></tr>
<tr><td>model</td><td><code>{e(str(info.get("model", "")))}</code>{(" · investigation " + e(str(info.get("investigate_model")))) if info.get("investigate_model") else ""}</td></tr>
<tr><td>steps</td><td>{info.get("steps", "")}</td></tr>
<tr><td>tokens</td><td>{(info.get("prompt_tokens") or 0) + (info.get("completion_tokens") or 0):,} in {info.get("model_calls", "")} calls</td></tr>
<tr><td>sandbox</td><td>{info.get("sandbox_commands", "")} commands · {len(run.checkpoints)} checkpoints · cost {info.get("sandbox_cost", 0):.4f}</td></tr>
<tr><td>images</td><td><code>{e(str(info.get("baseline_image", "")))}</code> → <code>{e(str(info.get("final_image", "")))}</code></td></tr>
<tr><td>elapsed</td><td>{int(info.get("elapsed_s") or 0)} s</td></tr>
</table>
{("<h2>Usage by model</h2><table><tr><th>model</th><th>calls</th><th>prompt</th><th>completion</th></tr>" + by_model + "</table>") if by_model else ""}
<h2>Summary</h2>
<div class="summary">{e(str(info.get("summary", "")))}</div>
<h2>Steps</h2>
<table><tr><th>step</th><th>tool</th><th>arguments</th><th>result</th></tr>{rows}</table>
<h2>Checkpoints</h2>
<p>Every command and edit produced an immutable state. Any of them can be diffed or restored with <code>praxbot inspect {e(run.run_id)} --diff N</code>.</p>
<table><tr><th>step</th><th>image</th><th>label</th></tr>{cps}</table>
<h2>Diff</h2>
<pre>{e(diff) if diff.strip() else "(no changes)"}</pre>
</body></html>
"""
