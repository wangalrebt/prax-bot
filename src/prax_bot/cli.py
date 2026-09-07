from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm
from rich.syntax import Syntax
from rich.table import Table

from . import __version__
from .settings import Settings, contree_auth_ini

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Prax Bot: a coding agent that works in a Nebius Token Factory Sandbox, powered by NVIDIA Nemotron.",
)
console = Console()

MAX_DIFF_LINES_SHOWN = 400
NO_SANDBOX_ACCESS = (
    "the sandbox API refused the request (403). Sandboxes are in beta and access is granted per project: "
    "request it at https://tokenfactory.nebius.com/sandboxes/about, or run with --backend local meanwhile."
)


def _repo_root(path: Path) -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise typer.BadParameter(f"{path} is not inside a git repository") from None
    return Path(out)


def _require_credentials(settings: Settings, *, need_project: bool = True) -> None:
    problems = settings.missing()
    if not need_project:
        problems = [p for p in problems if not p.startswith("NEBIUS_PROJECT_ID")]
    if not problems:
        return
    for p in problems:
        console.print(f"[red]✗[/] {escape(p)}")
    console.print("Set them, then run [bold]praxbot doctor[/].")
    raise typer.Exit(2)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def _apply_diff(root: Path, diff: str) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as f:
        f.write(diff)
        patch_path = f.name
    try:
        check = subprocess.run(["git", "apply", "--check", patch_path], cwd=root, capture_output=True, text=True)
        if check.returncode != 0:
            console.print(f"[red]the diff does not apply cleanly:[/] {escape(check.stderr.strip())}")
            return False
        applied = subprocess.run(["git", "apply", patch_path], cwd=root, capture_output=True, text=True)
        if applied.returncode != 0:
            console.print(f"[red]git apply failed:[/] {escape(applied.stderr.strip())}")
            return False
    finally:
        os.unlink(patch_path)
    status = subprocess.run(["git", "status", "--short"], cwd=root, capture_output=True, text=True).stdout
    console.print("[green]applied to the working tree[/] (nothing committed)")
    if status.strip():
        console.print(escape(status.rstrip()))
    return True


@app.command()
def run(
    task: str = typer.Argument(..., help="What to do, in plain language"),
    repo: Path = typer.Option(Path("."), "--repo", "-C", help="A path inside the git repository to work on"),
    model: Optional[str] = typer.Option(None, help="Model id on Token Factory. Default: Nemotron 3 Super"),
    investigate_model: Optional[str] = typer.Option(None, help="Model for the first investigation turns, e.g. Nemotron 3 Ultra. Default: same as --model"),
    base_image: Optional[str] = typer.Option(None, help="OCI image for the sandbox. Default: python:3.12"),
    setup: Optional[str] = typer.Option(None, help="Shell command to run once after upload, e.g. 'pip install -e . pytest'"),
    max_steps: Optional[int] = typer.Option(None, min=1, max=200, help="Model turns before the run is stopped (default 40)"),
    backend: str = typer.Option("contree", help="contree: a Token Factory Sandbox. local: this machine, for development only"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply the diff without asking"),
    export: Optional[Path] = typer.Option(None, help="Also write the diff to this file"),
    protect: list[str] = typer.Option([], "--protect", help="Glob the model may not edit, e.g. 'tests/*'. Repeatable"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show full tool outputs and checkpoints"),
) -> None:
    """Solve a task in the current repository. Runs entirely in a sandbox, then asks before applying the diff."""
    from contree_sdk import ContreeSync
    from contree_sdk.sdk.exceptions import ForbiddenError

    from .agent import Agent, AgentResult
    from .llm import LLM
    from .render import Reporter
    from .sandbox import WorkspaceError, create_workspace, make_client
    from .tools import ToolBox

    if backend not in {"contree", "local"}:
        raise typer.BadParameter("backend must be 'contree' or 'local'")
    settings = Settings.from_env(model=model, investigate_model=investigate_model, base_image=base_image, setup=setup, max_steps=max_steps)
    _require_credentials(settings, need_project=backend == "contree")
    root = _repo_root(repo)

    console.print(
        Panel.fit(
            f"[bold]Prax Bot[/] {__version__}\n"
            f"[dim]repo[/]   {root}\n[dim]model[/]  {settings.model}"
            + (f" (investigation: {settings.investigate_model}, first {settings.investigate_steps} steps)" if settings.investigate_model else "") + "\n"
            + (f"[dim]image[/]  {settings.base_image}" if backend == "contree" else "[dim]backend[/] local (this machine, no isolation)"),
            border_style="cyan",
        )
    )
    console.print(f"[bold]task[/]  {escape(task)}\n")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    run_dir = settings.home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    events_file = (run_dir / "events.jsonl").open("a", encoding="utf-8")
    reporter = Reporter(console, verbose=verbose)

    def on_event(kind: str, **payload: Any) -> None:
        record = {"t": time.time(), "kind": kind, **{k: _json_safe(v) for k, v in payload.items()}}
        events_file.write(json.dumps(record) + "\n")
        events_file.flush()
        reporter(kind, **payload)

    started = time.monotonic()
    console.print("[bold]sandbox[/]" if backend == "contree" else "[bold]local workspace[/]")
    try:
        if backend == "local":
            from .local import create_local_workspace

            ws = create_local_workspace(settings, root, on_event, run_id)
        else:
            client = make_client()
            ws = create_workspace(client, settings, root, on_event)
    except WorkspaceError as e:
        console.print(f"[red]✗ {escape(str(e))}[/]")
        raise typer.Exit(1) from None
    except ForbiddenError:
        console.print(f"[red]✗ {escape(NO_SANDBOX_ACCESS)}[/]")
        raise typer.Exit(1) from None
    except Exception as e:  # noqa: BLE001 - surface SDK errors with their type
        console.print(f"[red]✗ sandbox error: {type(e).__name__}: {escape(str(e))}[/]")
        raise typer.Exit(1) from None

    console.print("\n[bold]agent[/]")
    llm = LLM(settings)
    agent = Agent(
        llm, ToolBox(ws, protected=protect), max_steps=settings.max_steps, on_event=on_event,
        investigate_model=settings.investigate_model, investigate_steps=settings.investigate_steps,
    )
    try:
        result = agent.run(task)
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted; collecting the diff so far[/]")
        result = AgentResult(False, "interrupted by the user", 0, "interrupted")
    except Exception as e:  # noqa: BLE001 - keep the work that is already in the workspace
        console.print(f"\n[red]✗ the agent stopped: {type(e).__name__}: {escape(str(e)[:400])}[/]")
        result = AgentResult(False, f"stopped by an error: {type(e).__name__}: {str(e)[:200]}", 0, "error")

    diff: str | None = None
    diff_error: str | None = None
    try:
        diff = ws.diff()
        (run_dir / "diff.patch").write_text(diff, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        diff_error = f"{type(e).__name__}: {str(e)[:400]}"
    elapsed = time.monotonic() - started
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "task": task,
                "repo": str(root),
                "model": settings.model,
                "investigate_model": settings.investigate_model,
                "usage_by_model": llm.usage.by_model,
                "backend": backend,
                "base_image": settings.base_image if backend == "contree" else None,
                "finished": result.finished,
                "reason": result.reason,
                "steps": result.steps,
                "summary": result.summary,
                "model_calls": llm.usage.calls,
                "prompt_tokens": llm.usage.prompt_tokens,
                "completion_tokens": llm.usage.completion_tokens,
                "sandbox_commands": ws.commands,
                "sandbox_cost": ws.total_cost,
                "baseline_image": ws.checkpoints[0].image,
                "final_image": ws.image_id,
                "checkpoints": [dataclasses.asdict(c) for c in ws.checkpoints],
                "elapsed_s": elapsed,
                "diff_error": diff_error,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    events_file.close()
    if diff is None:
        console.print(f"[red]✗ could not extract the diff: {escape(diff_error or '')}[/]")
        console.print(f"the agent's final state is image {ws.image_id}; the log is in {run_dir}")
        raise typer.Exit(1)

    console.print()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("result", "[green]finished[/]" if result.finished else f"[yellow]stopped: {result.reason}[/]")
    table.add_row("steps", str(result.steps))
    table.add_row("tokens", f"{llm.usage.total_tokens:,} in {llm.usage.calls} calls")
    table.add_row("sandbox", f"{ws.commands} commands · {len(ws.checkpoints)} checkpoints · cost {ws.total_cost:.4f}")
    table.add_row("images", f"baseline {ws.checkpoints[0].image} → final {ws.image_id}")
    table.add_row("elapsed", f"{elapsed:.0f}s")
    table.add_row("log", str(run_dir))
    console.print(table)
    console.print(Panel(escape(result.summary or "(no summary)"), title="summary", border_style="green" if result.finished else "yellow"))

    if ws.dropped_scratch:
        console.print(f"[dim]left out of the diff, they look like scratch files: {escape(', '.join(ws.dropped_scratch))}[/]")
    if not diff.strip():
        console.print("no changes to the repository")
        return

    lines = diff.splitlines()
    shown = "\n".join(lines[:MAX_DIFF_LINES_SHOWN])
    console.print(Syntax(shown, "diff", theme="ansi_dark", word_wrap=False))
    if len(lines) > MAX_DIFF_LINES_SHOWN:
        console.print(f"[dim]… {len(lines) - MAX_DIFF_LINES_SHOWN} more lines in {run_dir / 'diff.patch'}[/]")
    if export:
        export.write_text(diff, encoding="utf-8")
        console.print(f"diff written to {export}")

    if yes:
        apply = True
    elif sys.stdin.isatty():
        apply = Confirm.ask("Apply this diff to the working tree?", default=False)
    else:
        apply = False
        console.print("[dim]no terminal to confirm on; pass --yes to apply automatically[/]")
    if apply:
        if not _apply_diff(root, diff):
            console.print(f"the diff is saved at {run_dir / 'diff.patch'}")
            raise typer.Exit(1)
    else:
        console.print(f"not applied. The diff is saved at {run_dir / 'diff.patch'}")
    if result.reason == "error":
        raise typer.Exit(1)


@app.command()
def doctor(
    base_image: Optional[str] = typer.Option(None, help="OCI image to test. Default: python:3.12"),
    skip_sandbox: bool = typer.Option(False, help="Skip the sandbox checks"),
) -> None:
    """Check credentials, the Nemotron model, tool calling, and the sandbox."""
    settings = Settings.from_env(base_image=base_image)
    ok = True

    def row(status: bool, name: str, detail: str = "") -> None:
        mark = "[green]✓[/]" if status else "[red]✗[/]"
        console.print(f"  {mark} {escape(name)}  [dim]{escape(detail)}[/]")

    console.print("[bold]credentials[/]")
    row(bool(settings.api_key), "NEBIUS_API_KEY", "set" if settings.api_key else "not set")
    project_ok = bool(settings.project_id)
    row(project_ok, "NEBIUS_PROJECT_ID", "set" if project_ok else "not set")
    if not settings.api_key or (not project_ok and not skip_sandbox):
        console.print(f"\n[dim]values are read from the environment or from {escape(str(contree_auth_ini()))}[/]")
        console.print("\nSet the missing variables and run doctor again.")
        raise typer.Exit(1)

    console.print("\n[bold]token factory[/]")
    from .llm import LLM

    llm = LLM(settings)
    try:
        models = llm.list_models()
        nvidia = [m for m in models if m.startswith("nvidia/")]
        row(True, "model list", f"{len(models)} models, {len(nvidia)} from nvidia")
        for m in nvidia:
            console.print(f"        {escape(m)}")
        available = settings.model in models
        row(available, f"configured model {settings.model}", "available" if available else "not in the list")
        ok &= available
    except Exception as e:  # noqa: BLE001
        row(False, "model list", f"{type(e).__name__}: {e}")
        ok = False

    ping_tool = [
        {
            "type": "function",
            "function": {
                "name": "ping",
                "description": "Reply with the given text.",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            },
        }
    ]
    try:
        t0 = time.monotonic()
        msg = llm.complete(
            [
                {"role": "system", "content": "You are a test harness. Use the tools you are given."},
                {"role": "user", "content": "Call the ping tool with text 'pong'."},
            ],
            ping_tool,
        )
        calls = getattr(msg, "tool_calls", None) or []
        good = False
        detail = "no tool call returned"
        if calls:
            args = json.loads(calls[0].function.arguments or "{}")
            good = calls[0].function.name == "ping" and args.get("text") == "pong"
            detail = f"{calls[0].function.name}({calls[0].function.arguments})"
        elif msg.content:
            detail = f"model answered in prose: {msg.content[:80]!r}"
        row(good, "tool-call round trip", f"{time.monotonic() - t0:.1f}s · {detail}")
        ok &= good
    except Exception as e:  # noqa: BLE001
        row(False, "tool-call round trip", f"{type(e).__name__}: {e}")
        ok = False

    if not skip_sandbox:
        console.print("\n[bold]sandbox[/]")
        from contree_sdk import ContreeSync
        from contree_sdk.sdk.exceptions import ForbiddenError

        try:
            from .sandbox import make_client

            client = make_client()
            info = client.get_token_info()
            limits = ", ".join(f"{k}={v}" for k, v in sorted(info.limits.items())) or "none reported"
            row(True, "auth", f"limits: {limits}")

            t0 = time.monotonic()
            base = client.images.oci(settings.base_image, timeout=settings.import_timeout)
            row(True, f"base image {settings.base_image}", f"{base.uuid} · {time.monotonic() - t0:.1f}s")

            t0 = time.monotonic()
            a = base.run(shell="echo hello > /tmp/a && cat /tmp/a", disposable=False, timeout=60).wait()
            ran = a.exit_code == 0 and "hello" in str(a.stdout or "")
            row(ran, "run a command", f"exit {a.exit_code} · {time.monotonic() - t0:.1f}s · new image {a.uuid}")
            ok &= ran

            b = base.run(shell="echo hello > /tmp/b", disposable=False, timeout=60).wait()
            a_sees_b = a.run(shell="test -e /tmp/b", disposable=True, timeout=60).wait().exit_code == 0
            b_sees_a = b.run(shell="test -e /tmp/a", disposable=True, timeout=60).wait().exit_code == 0
            isolated = not a_sees_b and not b_sees_a
            row(isolated, "fork isolation", "two runs from one image cannot see each other's files" if isolated else "files leaked between forks")
            ok &= isolated

            c = a.apply_files({"/tmp/c.txt": b"upload ok"})
            content = c.read("/tmp/c.txt")
            row(content == b"upload ok", "upload and read a file", f"{len(content)} bytes")
            ok &= content == b"upload ok"
        except ForbiddenError:
            row(False, "sandbox access", NO_SANDBOX_ACCESS)
            ok = False
        except Exception as e:  # noqa: BLE001
            row(False, "sandbox", f"{type(e).__name__}: {e}")
            ok = False

    console.print("\n[bold]local backend[/]")
    import shutil

    for tool in ("git", "uv", "bash"):
        row(shutil.which(tool) is not None, tool, shutil.which(tool) or "not found on PATH")

    console.print()
    console.print("[green]all checks passed[/]" if ok else "[red]some checks failed[/]")
    raise typer.Exit(0 if ok else 1)


@app.command()
def inspect(
    run_id: Optional[str] = typer.Argument(None, help="Run id; default: the latest run. Omit with --list to see recent runs"),
    diff: Optional[int] = typer.Option(None, "--diff", help="Show the diff of checkpoint N against the baseline"),
    apply_step: Optional[int] = typer.Option(None, "--apply", help="Apply the state of checkpoint N to the working tree"),
    repo: Path = typer.Option(Path("."), "--repo", "-C", help="Repository to apply into"),
    list_runs_: bool = typer.Option(False, "--list", help="List recent runs"),
) -> None:
    """Look at a past run: its steps, its checkpoint chain, and the diff at any checkpoint."""
    from .runs import checkpoint_diff, latest_run_id, list_runs, load_run

    settings = Settings.from_env()
    if list_runs_ or run_id is None and diff is None and apply_step is None and latest_run_id(settings) is None:
        runs = list_runs(settings, limit=15)
        if not runs:
            console.print(f"no runs under {settings.home / 'runs'}")
            return
        table = Table(box=None, padding=(0, 2))
        for col in ("run", "result", "steps", "tokens", "backend", "task"):
            table.add_column(col)
        for r in runs:
            i = r.info
            table.add_row(
                r.run_id, "finished" if i.get("finished") else str(i.get("reason", "?")), str(i.get("steps", "")),
                f"{(i.get('prompt_tokens') or 0) + (i.get('completion_tokens') or 0):,}", str(i.get("backend", "")),
                escape(str(i.get("task", ""))[:60]),
            )
        console.print(table)
        return

    run_id = run_id or latest_run_id(settings)
    try:
        run = load_run(run_id, settings)  # type: ignore[arg-type]
    except FileNotFoundError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(1) from None

    if diff is not None or apply_step is not None:
        step = diff if diff is not None else apply_step
        try:
            text = checkpoint_diff(run, int(step), settings)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]could not compute the diff: {type(e).__name__}: {escape(str(e)[:300])}[/]")
            raise typer.Exit(1) from None
        if not text.strip():
            console.print(f"checkpoint {step} has no changes against the baseline")
            return
        if diff is not None:
            console.print(Syntax(text, "diff", theme="ansi_dark", word_wrap=False))
            return
        root = _repo_root(repo)
        console.print(f"applying the state of checkpoint {step} ({escape(run.checkpoints[int(step)]['label'])}) to {root}")
        if not _apply_diff(root, text):
            raise typer.Exit(1)
        return

    i = run.info
    console.print(Panel.fit(
        f"[bold]run[/] {run.run_id}\n[dim]task[/]     {escape(str(i.get('task', '')))}\n"
        f"[dim]result[/]   {'finished' if i.get('finished') else 'stopped: ' + str(i.get('reason', ''))}\n"
        f"[dim]model[/]    {escape(str(i.get('model', '')))}   [dim]backend[/] {i.get('backend', '')}\n"
        f"[dim]tokens[/]   {(i.get('prompt_tokens') or 0) + (i.get('completion_tokens') or 0):,}   [dim]elapsed[/] {int(i.get('elapsed_s') or 0)} s",
        border_style="cyan",
    ))
    steps = Table(box=None, padding=(0, 2))
    for col in ("step", "tool", "arguments", "result"):
        steps.add_column(col)
    from .render import describe_call

    for c in run.tool_calls:
        style = "red" if c.get("error") else ""
        steps.add_row(str(c["step"]), str(c["name"]), escape(describe_call(str(c["name"]), str(c["args"]))[:70]), f"[{style}]{escape(str(c['result'])[:80])}[/]" if style else escape(str(c["result"])[:80]))
    console.print(steps)
    cps = Table(box=None, padding=(0, 2), title="checkpoints", title_justify="left")
    for col in ("step", "image", "label"):
        cps.add_column(col)
    for c in run.checkpoints:
        cps.add_row(str(c["step"]), str(c["image"]), escape(str(c["label"])))
    console.print(cps)
    console.print(f"[dim]praxbot inspect {run.run_id} --diff N shows any checkpoint's diff; --apply N applies it[/]")


@app.command()
def report(
    run_id: Optional[str] = typer.Argument(None, help="Run id; default: the latest run"),
    out: Optional[Path] = typer.Option(None, help="Where to write the HTML; default: report.html in the run directory"),
    open_: bool = typer.Option(False, "--open", help="Open the report in a browser"),
) -> None:
    """Write a self-contained HTML report for a run."""
    from .runs import latest_run_id, load_run, render_html

    settings = Settings.from_env()
    run_id = run_id or latest_run_id(settings)
    if run_id is None:
        console.print("no runs yet")
        raise typer.Exit(1)
    run = load_run(run_id, settings)
    target = out or (run.directory / "report.html")
    target.write_text(render_html(run), encoding="utf-8")
    console.print(f"report written to {target}")
    if open_:
        import webbrowser

        webbrowser.open(target.as_uri())


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"praxbot {__version__}")
