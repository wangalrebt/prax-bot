"""Run history, saved diffs, and reports."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from prax_bot import __version__
from prax_bot.settings import Settings
from prax_bot.ui.common import (
    console,
    repo_root,
)
from prax_bot.workspace.repository import apply_diff
from prax_bot.workspace.types import WorkspaceError


def status(
    limit: int = typer.Option(10, "--limit", "-n", min=1, max=100, help="How many recent runs to list"),
) -> None:
    """What Prax Bot is doing now, what it did recently, and whether it is set up."""
    from prax_bot.runtime.runs import ago, status_summary

    settings = Settings.from_env()
    missing = settings.missing()
    summary = status_summary(settings, limit=limit)
    runs = summary["runs"]
    lines = [
        f"[bold]Prax Bot[/] {__version__}",
        f"[dim]model[/]    {settings.model}",
        f"[dim]sandbox[/]  project {settings.project_id}"
        if settings.project_id
        else "[dim]sandbox[/]  no project id; only --backend local will work",
        "[dim]auth[/]     ok" if not missing else f"[dim]auth[/]     [red]missing:[/] {escape('; '.join(missing))}",
        f"[dim]runs[/]     {summary['total_runs']} under {settings.home / 'runs'}",
    ]
    if summary["local_workspaces"]:
        lines.append(
            f"[dim]local[/]    {summary['local_workspaces']} workspaces, {summary['local_bytes'] / 1e9:.1f} GB under {settings.home / 'local'}"
        )
    console.print(Panel.fit("\n".join(lines), border_style="cyan"))

    active = [r for r in runs if r.state == "in progress"]
    for r in active:
        console.print(
            f"[bold green]in progress[/]  {r.run_id}  started {ago(r.started)}, last activity {ago(r.last_activity)}"
            + (f", last tool {escape(r.last_tool)}" if r.last_tool else "")
            + f"\n  {escape(r.task[:100])}"
        )
    if not runs:
        console.print('no runs yet. Start one with [bold]praxbot run "<task>"[/].')
        return
    table = Table(box=None, padding=(0, 2), header_style="dim")
    for col in (
        "when",
        "kind",
        "task",
        "status",
        "recorded",
        "last log",
        "steps",
        "tokens",
        "diff",
        "applied",
        "run id",
    ):
        table.add_column(col, no_wrap=col != "task")
    for r in runs:
        result = {
            "finished": "[green]finished[/]",
            "in progress": "[green]in progress[/]",
            "unknown": "[dim]unknown[/]",
            "inactive": "[dim]inactive[/]",
        }.get(r.state, f"[yellow]{escape(r.state)}[/]")
        if r.goal:
            result += " · goal met" if r.goal.get("met") else f" · goal not met ({r.goal.get('rounds', 0)} rounds)"
        table.add_row(
            ago(r.started),
            r.kind,
            escape(r.task[:60] + ("…" if len(r.task) > 60 else "")),
            result,
            r.last_recorded_state or "",
            ago(r.last_activity),
            "" if r.steps is None else str(r.steps),
            "" if r.tokens is None else f"{r.tokens:,}",
            f"{r.files_changed} file{'s' if r.files_changed != 1 else ''}" if r.files_changed else "none",
            {True: "yes", False: "no"}.get(r.applied, ""),
            r.run_id,
        )
    console.print(table)
    console.print(
        "[dim]Live status uses the event writer's lock; saved state and log age alone do not prove a run is live.[/]"
    )
    console.print("[dim]praxbot inspect <run id> for the steps; praxbot resume <run id> to continue one.[/]")


def inspect(
    run_id: str | None = typer.Argument(
        None, help="Run id; default: the latest run. Omit with --list to see recent runs"
    ),
    diff: int | None = typer.Option(None, "--diff", help="Show the diff of checkpoint N against the baseline"),
    apply_step: int | None = typer.Option(None, "--apply", help="Apply the state of checkpoint N to the working tree"),
    repo: Path = typer.Option(Path("."), "--repo", "-C", help="Repository to apply into"),
    list_runs_: bool = typer.Option(False, "--list", help="List recent runs"),
) -> None:
    """Look at a past run: its steps, its checkpoint chain, and the diff at any checkpoint."""
    from prax_bot.runtime.runs import checkpoint_diff, latest_run_id, list_runs, load_run

    settings = Settings.from_env()
    if list_runs_ or (run_id is None and diff is None and apply_step is None and latest_run_id(settings) is None):
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
                r.run_id,
                "finished" if i.get("finished") else str(i.get("reason", "?")),
                str(i.get("steps", "")),
                f"{(i.get('prompt_tokens') or 0) + (i.get('completion_tokens') or 0):,}",
                str(i.get("backend", "")),
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
        except Exception as e:
            console.print(f"[red]could not compute the diff: {type(e).__name__}: {escape(str(e)[:300])}[/]")
            raise typer.Exit(1) from None
        if not text.strip():
            console.print(f"checkpoint {step} has no changes against the baseline")
            return
        if diff is not None:
            console.print(Syntax(text, "diff", theme="ansi_dark", word_wrap=False))
            return
        root = repo_root(repo)
        console.print(
            f"applying the state of checkpoint {step} ({escape(run.checkpoints[int(step)]['label'])}) to {root}"
        )
        try:
            apply_diff(root, text)
        except WorkspaceError as error:
            console.print(f"[red]{escape(str(error))}[/]")
            raise typer.Exit(1) from None
        console.print("applied to the working tree")
        return

    i = run.info
    console.print(
        Panel.fit(
            f"[bold]run[/] {run.run_id}\n[dim]task[/]     {escape(str(i.get('task', '')))}\n"
            f"[dim]result[/]   {'finished' if i.get('finished') else 'stopped: ' + str(i.get('reason', ''))}\n"
            f"[dim]model[/]    {escape(str(i.get('model', '')))}   [dim]backend[/] {i.get('backend', '')}\n"
            f"[dim]tokens[/]   {(i.get('prompt_tokens') or 0) + (i.get('completion_tokens') or 0):,}   [dim]elapsed[/] {int(i.get('elapsed_s') or 0)} s",
            border_style="cyan",
        )
    )
    steps = Table(box=None, padding=(0, 2))
    for col in ("step", "tool", "arguments", "result"):
        steps.add_column(col)
    from prax_bot.ui.render import describe_call

    for c in run.tool_calls:
        style = "red" if c.get("error") else ""
        steps.add_row(
            str(c["step"]),
            str(c["name"]),
            escape(describe_call(str(c["name"]), str(c["args"]))[:70]),
            f"[{style}]{escape(str(c['result'])[:80])}[/]" if style else escape(str(c["result"])[:80]),
        )
    console.print(steps)
    cps = Table(box=None, padding=(0, 2), title="checkpoints", title_justify="left")
    for col in ("step", "image", "label"):
        cps.add_column(col)
    for c in run.checkpoints:
        cps.add_row(str(c["step"]), str(c["image"]), escape(str(c["label"])))
    console.print(cps)
    console.print(f"[dim]praxbot inspect {run.run_id} --diff N shows any checkpoint's diff; --apply N applies it[/]")


def report(
    run_id: str | None = typer.Argument(None, help="Run id; default: the latest run"),
    out: Path | None = typer.Option(None, help="Where to write the HTML; default: report.html in the run directory"),
    open_: bool = typer.Option(False, "--open", help="Open the report in a browser"),
) -> None:
    """Write a self-contained HTML report for a run."""
    from prax_bot.runtime.runs import latest_run_id, load_run
    from prax_bot.ui.report import render_html

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
