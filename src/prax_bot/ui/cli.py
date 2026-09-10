"""The command line: every prax / praxbot command, built on the session runtime in session.py."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm
from rich.syntax import Syntax
from rich.table import Table

from prax_bot import __version__
from prax_bot.runtime.session import (
    NO_SANDBOX_ACCESS,
    Session,
    agent_round,
    apply_diff,
    build_record,
    build_settings,
    console,
    load_extensions,
    new_run,
    open_workspace,
    print_diff,
    print_result,
    sampling_label,
    write_record,
)
from prax_bot.settings import Settings, contree_auth_ini

# Options shared by the commands that open a workspace, declared once so their help text cannot drift.
RepoOpt = Annotated[Path, typer.Option("--repo", "-C", help="A path inside the git repository to work on")]
ModelOpt = Annotated[str | None, typer.Option(help="Model id on Token Factory. Default: Nemotron 3 Super")]
BaseImageOpt = Annotated[str | None, typer.Option(help="OCI image for the sandbox. Default: python:3.12")]
SetupOpt = Annotated[
    str | None, typer.Option(help="Shell command to run once after upload, e.g. 'pip install -e . pytest'")
]
MaxStepsOpt = Annotated[
    int | None, typer.Option(min=1, max=200, help="Model turns per round before it is stopped (default 40)")
]
BackendOpt = Annotated[
    str | None,
    typer.Option(
        help="contree: a Token Factory Sandbox. local: this machine, for development only. Default: the config file's backend, else contree"
    ),
]
YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Apply the diff without asking")]
ExportOpt = Annotated[Path | None, typer.Option(help="Also write the diff to this file")]
ProtectOpt = Annotated[
    list[str], typer.Option("--protect", help="Glob the model may not edit, e.g. 'tests/*'. Repeatable")
]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Show full tool outputs and checkpoints")]
TemperatureOpt = Annotated[
    float | None,
    typer.Option(min=0.0, max=2.0, help="Sampling temperature (default 1.0, NVIDIA's recommendation for Nemotron 3)"),
]
TopPOpt = Annotated[float | None, typer.Option("--top-p", min=0.0, max=1.0, help="Nucleus sampling (default 0.95)")]
ContextBudgetOpt = Annotated[
    int | None,
    typer.Option(
        "--context-budget",
        min=10_000,
        help="Tokens of context per model call before the transcript is summarised (default 80,000)",
    ),
]
EffortOpt = Annotated[
    str | None, typer.Option(help="How much the model reasons per step: off, low (default), high, or xhigh")
]

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="Prax Bot: a coding agent that works in a Nebius Token Factory Sandbox, powered by NVIDIA Nemotron.",
)

INIT_TASK = (
    "{what}: a short guide for coding agents working in this repository. Cover, in this order: what the project is "
    "in one sentence; how to install its dependencies; how to run the tests, and the linters or type checkers if it has any; "
    "the layout of the code (which directories hold what); conventions worth knowing (style, naming, where tests go, "
    "commit or changelog rules if documented); and anything surprising a newcomer would trip over. Keep it under 80 lines. "
    "Base every command and statement on files you actually read (pyproject, setup files, CI configs, Makefiles, READMEs, "
    "existing tests); run the test command once to confirm it works, and never invent commands. "
    "Change no file other than AGENTS.md."
)


def _repo_root(path: Path) -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise typer.BadParameter(f"{path} is not inside a git repository") from None
    return Path(out)


def _maybe_repo_root(path: Path) -> Path | None:
    """The repository root, or None when not inside one; for commands that work either way."""
    try:
        return _repo_root(path)
    except typer.BadParameter:
        return None


STARTER_USER_CONFIG = """# Prax Bot settings. A flag or a PRAXBOT_* environment variable overrides anything here.
# model = "nvidia/nemotron-3-super-120b-a12b"
# effort = "low"            # off, low, high, xhigh
# backend = "contree"       # contree: a Token Factory sandbox; local: this machine, for development
# base_image = "python:3.12"
# max_steps = 40
# temperature = 1.0
# top_p = 0.95
"""

STARTER_REPO_CONFIG = """# Prax Bot settings for this repository; they override ~/.praxbot/config.toml.
# A repository cannot choose the backend, and its setup command only runs inside a sandbox.
# setup = "pip install -e . pytest"   # runs once after the upload, before the agent starts
# base_image = "python:3.12"
# max_steps = 40
# effort = "low"
"""


@app.command()
def config(
    init: bool = typer.Option(False, "--init", help="Write a starter ~/.praxbot/config.toml"),
    init_repo: bool = typer.Option(
        False, "--init-repo", help="Write a starter .praxbot/config.toml in this repository"
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file"),
) -> None:
    """Show the effective settings and where each one comes from; --init writes a starter file to edit."""
    from prax_bot.settings import CONFIG_FILE, REPO_CONFIG_FILE

    root = _maybe_repo_root(Path("."))
    settings = build_settings(repo_root=root)
    if init or init_repo:
        if init_repo and root is None:
            console.print("not inside a git repository; --init-repo needs one")
            raise typer.Exit(1)
        path = settings.home / CONFIG_FILE if init else root / REPO_CONFIG_FILE  # type: ignore[operator]
        if path.exists() and not force:
            console.print(f"{path} already exists; pass --force to overwrite it")
            raise typer.Exit(1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STARTER_USER_CONFIG if init else STARTER_REPO_CONFIG, encoding="utf-8")
        console.print(f"wrote {path}; every line is a comment, uncomment what you want to set")
        return
    table = Table(box=None, padding=(0, 2), header_style="dim")
    for column in ("setting", "value", "from"):
        table.add_column(column)
    for key, value, source in settings.effective():
        table.add_row(key, escape(value) if value else "[dim]unset[/]", source)
    console.print(table)
    files = ", ".join(str(f) for f in settings.config_files)
    console.print(f"[dim]config files: {escape(files) if files else 'none; prax config --init writes one'}[/]")
    console.print(
        "[dim]precedence: flag, PRAXBOT_* environment, repository .praxbot/config.toml, ~/.praxbot/config.toml, default[/]"
    )


@app.command()
def prompt(
    path: bool = typer.Option(False, "--path", help="Print where the prompt comes from instead of its text"),
) -> None:
    """Print the system prompt in effect: the built-in one, or the file set as system_prompt_file."""
    from prax_bot.core.agent import DEFAULT_SYSTEM_PROMPT_PATH, SYSTEM_PROMPT

    settings = build_settings(repo_root=_maybe_repo_root(Path(".")))
    custom = settings.system_prompt_text()
    if path:
        console.print(
            str(Path(settings.system_prompt_file).expanduser()) if custom else f"built in: {DEFAULT_SYSTEM_PROMPT_PATH}"
        )
        console.print(
            "[dim]replace it with system_prompt_file in ~/.praxbot/config.toml or PRAXBOT_SYSTEM_PROMPT_FILE; project rules belong in AGENTS.md[/]"
        )
        return
    print(custom or SYSTEM_PROMPT)


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


def _ask_followup() -> str:
    """A further instruction typed by the user, or an empty string to stop."""
    from rich.prompt import Prompt

    try:
        return Prompt.ask("[bold]follow-up[/] [dim](Enter to finish)[/]", default="", show_default=False).strip()
    except (KeyboardInterrupt, EOFError):
        console.print()
        return ""


def _solve(
    settings: Settings,
    ws: Any,
    root: Path,
    task: str,
    *,
    backend: str,
    run_id: str,
    run_dir: Path,
    events_file: Any,
    on_event: Any,
    yes: bool,
    export: Path | None,
    protect: list[str],
    started: float,
    resumed_from: dict[str, Any] | None = None,
    record_task: str | None = None,
    ask_followup: Any = None,
    auto_next: Any = None,
    extra: Any = None,
    goal: bool = False,
    skills: Any = (),
    plugins: Any = (),
) -> None:
    """Run the agent on the workspace, show the diff, take follow-up instructions, then offer to apply.

    Each follow-up starts a new agent transcript on the same workspace: the task text carries the original
    request, the previous summary and the diff so far, and the sandbox state carries everything else.
    """
    from prax_bot.core.llm import LLM
    from prax_bot.runtime.runs import continuation_task

    if ask_followup is None:
        ask_followup = _ask_followup if sys.stdin.isatty() and not yes else (lambda: "")
    llm = LLM(settings)
    original_task = task
    followups: list[str] = []
    steps_total = 0
    round_no = 0
    applied_ok: bool | None = None  # recorded once the user decides; None while undecided or with no diff

    def save(result: Any, diff_error: str | None, applied: bool | None) -> None:
        write_record(
            run_dir,
            build_record(
                run_id=run_id,
                task=record_task or original_task,
                followups=followups,
                repo=root,
                settings=settings,
                llm=llm,
                ws=ws,
                backend=backend,
                base_image=settings.base_image if backend == "contree" and not resumed_from else None,
                result=result,
                steps=steps_total,
                summary=result.summary,
                elapsed_s=time.monotonic() - started,
                resumed_from=resumed_from,
                diff_error=diff_error,
                applied=applied,
                extra=extra() if callable(extra) else (extra or {}),
            ),
        )

    while True:
        round_no += 1
        result = agent_round(
            settings, ws, task, llm, on_event, protect=protect, skills=skills, plugins=plugins, goal=goal
        )
        steps_total += result.steps

        diff: str | None = None
        diff_error: str | None = None
        try:
            diff = ws.diff()
            (run_dir / "diff.patch").write_text(diff, encoding="utf-8")
        except Exception as e:
            diff_error = f"{type(e).__name__}: {str(e)[:400]}"

        save(result, diff_error, applied_ok)
        if diff is None:
            events_file.close()
            console.print(f"[red]✗ could not extract the diff: {escape(diff_error or '')}[/]")
            console.print(f"the agent's final state is image {ws.image_id}; the log is in {run_dir}")
            raise typer.Exit(1)

        print_result(
            result,
            llm,
            ws,
            run_dir,
            steps_total=steps_total,
            round_steps=result.steps if followups else None,
            elapsed=time.monotonic() - started,
        )
        print_diff(diff, ws, run_dir, export)

        more = auto_next(round_no, result, diff) if auto_next else None
        if more:
            console.print(f"\n[bold]next round[/]  {escape(more.splitlines()[0])}")
        else:
            more = ask_followup()
        if not more:
            break
        followups.append(more)
        on_event("followup", instruction=more)
        task = continuation_task(original_task, result.summary, diff, more)

    save(result, diff_error, applied_ok)  # the last round's verdict, e.g. a goal met, is decided after the record above
    events_file.close()
    if not diff.strip():
        return
    if yes:
        apply = True
    elif sys.stdin.isatty():
        apply = Confirm.ask("Apply this diff to the working tree?", default=False)
    else:
        apply = False
        console.print("[dim]no terminal to confirm on; pass --yes to apply automatically[/]")
    if apply:
        applied_ok = apply_diff(root, diff)
        save(result, diff_error, applied_ok)
        if not applied_ok:
            console.print(f"the diff is saved at {run_dir / 'diff.patch'}")
            raise typer.Exit(1)
    else:
        applied_ok = False
        save(result, diff_error, applied_ok)
        console.print(f"not applied. The diff is saved at {run_dir / 'diff.patch'}")
    if result.reason == "error":
        raise typer.Exit(1)


def _start_session(
    repo: Path,
    model: str | None,
    base_image: str | None,
    setup: str | None,
    max_steps: int | None,
    backend: str,
    protect: list[str],
    verbose: bool,
    temperature: float | None,
    top_p: float | None,
    effort: str | None,
    context_budget: int | None = None,
) -> None:
    root = _repo_root(repo)
    settings = build_settings(
        model=model,
        base_image=base_image,
        setup=setup,
        max_steps=max_steps,
        temperature=temperature,
        top_p=top_p,
        effort=effort,
        context_budget=context_budget,
        backend=backend,
        repo_root=root,
    )
    backend = settings.backend
    _require_credentials(settings, need_project=backend == "contree")
    _header(settings, root, backend, "\n[dim]mode[/]   session: one sandbox, many instructions")
    skills, plugins = load_extensions(settings)
    run_id, run_dir, events_file, on_event = new_run(settings, verbose)
    ws = open_workspace(settings, backend, root, run_id, on_event)
    Session(
        settings,
        ws,
        root,
        backend=backend,
        run_id=run_id,
        run_dir=run_dir,
        events_file=events_file,
        on_event=on_event,
        protect=protect,
        skills=skills,
        plugins=plugins,
    ).loop()


@app.command()
def session(
    repo: RepoOpt = Path("."),
    model: ModelOpt = None,
    base_image: BaseImageOpt = None,
    setup: SetupOpt = None,
    max_steps: MaxStepsOpt = None,
    backend: BackendOpt = None,
    protect: ProtectOpt = (),
    verbose: VerboseOpt = False,
    temperature: TemperatureOpt = None,
    top_p: TopPOpt = None,
    effort: EffortOpt = None,
    context_budget: ContextBudgetOpt = None,
) -> None:
    """Open one sandbox on this repository and keep giving it instructions. Also what bare `praxbot` does."""
    _start_session(
        repo, model, base_image, setup, max_steps, backend, protect, verbose, temperature, top_p, effort, context_budget
    )


@app.command()
def ui(
    repo: RepoOpt = Path("."),
    model: ModelOpt = None,
    base_image: BaseImageOpt = None,
    setup: SetupOpt = None,
    max_steps: MaxStepsOpt = None,
    backend: BackendOpt = None,
    protect: ProtectOpt = (),
    verbose: VerboseOpt = False,
    temperature: TemperatureOpt = None,
    top_p: TopPOpt = None,
    effort: EffortOpt = None,
    context_budget: ContextBudgetOpt = None,
) -> None:
    """The full-screen interface: conversation, sidebar, diff pane, one input line. Also what bare `praxbot` does on a terminal."""
    _start_ui(
        repo, model, base_image, setup, max_steps, backend, protect, verbose, temperature, top_p, effort, context_budget
    )


def _start_ui(
    repo: Path,
    model: str | None,
    base_image: str | None,
    setup: str | None,
    max_steps: int | None,
    backend: str,
    protect: list[str],
    verbose: bool,
    temperature: float | None,
    top_p: float | None,
    effort: str | None,
    context_budget: int | None = None,
) -> None:
    from prax_bot.ui.tui import run_ui

    root = _repo_root(repo)
    settings = build_settings(
        model=model,
        base_image=base_image,
        setup=setup,
        max_steps=max_steps,
        temperature=temperature,
        top_p=top_p,
        effort=effort,
        context_budget=context_budget,
        backend=backend,
        repo_root=root,
    )
    backend = settings.backend
    _require_credentials(settings, need_project=backend == "contree")
    skills, plugins = load_extensions(settings)
    run_ui(settings, root, backend, protect, skills, plugins, verbose)


@app.command()
def web(
    repo: RepoOpt = Path("."),
    port: int = typer.Option(0, "--port", min=0, max=65535, help="Port on localhost; 0 picks a free one"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the URL instead of opening a browser"),
    model: ModelOpt = None,
    base_image: BaseImageOpt = None,
    setup: SetupOpt = None,
    max_steps: MaxStepsOpt = None,
    backend: BackendOpt = None,
    protect: ProtectOpt = (),
    verbose: VerboseOpt = False,
    temperature: TemperatureOpt = None,
    top_p: TopPOpt = None,
    effort: EffortOpt = None,
    context_budget: ContextBudgetOpt = None,
) -> None:
    """The web interface on localhost: chat with tool calls, changes, to-dos and rounds in a sidebar, a slash-command menu."""

    root = _repo_root(repo)
    settings = build_settings(
        model=model,
        base_image=base_image,
        setup=setup,
        max_steps=max_steps,
        temperature=temperature,
        top_p=top_p,
        effort=effort,
        context_budget=context_budget,
        backend=backend,
        repo_root=root,
    )
    backend = settings.backend
    _require_credentials(settings, need_project=backend == "contree")
    _start_web(settings, root, backend, protect, verbose, port, not no_browser)


def _start_web(
    settings: Settings, root: Path, backend: str, protect: list[str], verbose: bool, port: int, open_browser: bool
) -> None:
    from prax_bot.ui.web import WebSession, serve

    skills, plugins = load_extensions(settings)

    def make_session(out: Console, hook: Any) -> Session:
        run_id, run_dir, events_file, on_event = new_run(settings, verbose, out=out)

        def on_event_with_hook(kind: str, **payload: Any) -> None:
            on_event(kind, **payload)
            hook(kind, **payload)

        out.print(f"sandbox {settings.base_image}" if backend == "contree" else "local workspace")
        ws = open_workspace(settings, backend, root, run_id, on_event_with_hook, out=out)
        return Session(
            settings,
            ws,
            root,
            backend=backend,
            run_id=run_id,
            run_dir=run_dir,
            events_file=events_file,
            on_event=on_event_with_hook,
            protect=protect,
            skills=skills,
            plugins=plugins,
            read_line=lambda: None,
            out=out,
        )

    serve(WebSession(settings, root, backend, make_session), port=port, open_browser=open_browser)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Prax Bot: a coding agent that works in a Nebius Token Factory Sandbox, powered by NVIDIA Nemotron. With no command, opens the web interface."""
    if ctx.invoked_subcommand is None:
        root = _repo_root(Path("."))
        settings = build_settings(repo_root=root)
        _require_credentials(settings, need_project=settings.backend == "contree")
        _start_web(settings, root, settings.backend, [], False, 0, True)


@app.command()
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
    for col in ("when", "kind", "task", "result", "steps", "tokens", "diff", "applied", "run id"):
        table.add_column(col, no_wrap=col != "task")
    for r in runs:
        result = {
            "finished": "[green]finished[/]",
            "in progress": "[green]in progress[/]",
            "abandoned": "[dim]abandoned[/]",
        }.get(r.state, f"[yellow]{escape(r.state)}[/]")
        if r.goal:
            result += " · goal met" if r.goal.get("met") else f" · goal not met ({r.goal.get('rounds', 0)} rounds)"
        table.add_row(
            ago(r.started),
            r.kind,
            escape(r.task[:60] + ("…" if len(r.task) > 60 else "")),
            result,
            "" if r.steps is None else str(r.steps),
            "" if r.tokens is None else f"{r.tokens:,}",
            f"{r.files_changed} file{'s' if r.files_changed != 1 else ''}" if r.files_changed else "none",
            {True: "yes", False: "no"}.get(r.applied, ""),
            r.run_id,
        )
    console.print(table)
    console.print("[dim]praxbot inspect <run id> for the steps; praxbot resume <run id> to continue one.[/]")


@app.command()
def run(
    task: str = typer.Argument(..., help="What to do, in plain language"),
    repo: RepoOpt = Path("."),
    model: ModelOpt = None,
    investigate_model: str | None = typer.Option(
        None, help="Model for the first investigation turns, e.g. Nemotron 3 Ultra. Default: same as --model"
    ),
    base_image: BaseImageOpt = None,
    setup: SetupOpt = None,
    max_steps: MaxStepsOpt = None,
    backend: BackendOpt = None,
    yes: YesOpt = False,
    export: ExportOpt = None,
    protect: ProtectOpt = (),
    verbose: VerboseOpt = False,
    temperature: TemperatureOpt = None,
    top_p: TopPOpt = None,
    effort: EffortOpt = None,
    context_budget: ContextBudgetOpt = None,
) -> None:
    """Solve a task in the current repository. Runs entirely in a sandbox, shows the diff, takes follow-up instructions, then asks before applying."""
    root = _repo_root(repo)
    settings = build_settings(
        model=model,
        investigate_model=investigate_model,
        base_image=base_image,
        setup=setup,
        max_steps=max_steps,
        temperature=temperature,
        top_p=top_p,
        effort=effort,
        context_budget=context_budget,
        backend=backend,
        repo_root=root,
    )
    backend = settings.backend
    _require_credentials(settings, need_project=backend == "contree")

    _header(settings, root, backend)
    console.print(f"[bold]task[/]  {escape(task)}")
    skills, plugins = load_extensions(settings)
    console.print()

    run_id, run_dir, events_file, on_event = new_run(settings, verbose)
    started = time.monotonic()
    ws = open_workspace(settings, backend, root, run_id, on_event)
    _solve(
        settings,
        ws,
        root,
        task,
        backend=backend,
        run_id=run_id,
        run_dir=run_dir,
        events_file=events_file,
        on_event=on_event,
        yes=yes,
        export=export,
        protect=protect,
        started=started,
        skills=skills,
        plugins=plugins,
    )


@app.command()
def init(
    repo: RepoOpt = Path("."),
    force: bool = typer.Option(False, "--force", help="Write AGENTS.md even if one exists"),
    model: ModelOpt = None,
    base_image: BaseImageOpt = None,
    setup: SetupOpt = None,
    max_steps: MaxStepsOpt = None,
    backend: BackendOpt = None,
    yes: bool = typer.Option(False, "--yes", "-y", help="Write the file without asking"),
    verbose: VerboseOpt = False,
) -> None:
    """Write an AGENTS.md for this repository: how to install, test and lint it, its layout and conventions, from what the agent finds."""
    root = _repo_root(repo)
    settings = build_settings(
        model=model, base_image=base_image, setup=setup, max_steps=max_steps, backend=backend, repo_root=root
    )
    backend = settings.backend
    _require_credentials(settings, need_project=backend == "contree")
    if (root / "AGENTS.md").exists() and not force:
        console.print(f"{root / 'AGENTS.md'} already exists; pass --force to replace it, or edit it by hand.")
        raise typer.Exit(1)
    task = INIT_TASK.format(
        what="Replace the existing AGENTS.md at the repository root"
        if force
        else "Create AGENTS.md at the repository root"
    )
    _header(settings, root, backend)
    console.print("[bold]task[/]  write AGENTS.md")
    skills, plugins = load_extensions(settings)
    console.print()
    run_id, run_dir, events_file, on_event = new_run(settings, verbose)
    started = time.monotonic()
    ws = open_workspace(settings, backend, root, run_id, on_event)
    _solve(
        settings,
        ws,
        root,
        task,
        backend=backend,
        run_id=run_id,
        run_dir=run_dir,
        events_file=events_file,
        on_event=on_event,
        yes=yes,
        export=None,
        protect=[],
        started=started,
        record_task="init: write AGENTS.md",
        skills=skills,
        plugins=plugins,
    )


def _header(settings: Settings, root: Path, backend: str, extra: str = "") -> None:
    console.print(
        Panel.fit(
            f"[bold]Prax Bot[/] {__version__}\n"
            f"[dim]repo[/]   {root}\n[dim]model[/]  {settings.model}"
            + (
                f" (investigation: {settings.investigate_model}, first {settings.investigate_steps} steps)"
                if settings.investigate_model
                else ""
            )
            + f" · {sampling_label(settings)}\n"
            + (
                f"[dim]image[/]  {settings.base_image}"
                if backend == "contree"
                else "[dim]backend[/] local (this machine, no isolation)"
            )
            + extra,
            border_style="cyan",
        )
    )


@app.command()
def goal(
    objective: str = typer.Argument(..., help="What must be true when the run ends, in plain language"),
    check: str | None = typer.Option(
        None,
        "--check",
        help="Shell command that exits 0 when the goal is met, e.g. 'pytest -q'. Without it the model decides",
    ),
    rounds: int = typer.Option(
        5, "--rounds", min=1, max=50, help="Most rounds to run; each round is a fresh transcript on the same workspace"
    ),
    repo: RepoOpt = Path("."),
    model: ModelOpt = None,
    base_image: BaseImageOpt = None,
    setup: SetupOpt = None,
    max_steps: MaxStepsOpt = None,
    backend: BackendOpt = None,
    yes: YesOpt = False,
    export: ExportOpt = None,
    protect: ProtectOpt = (),
    verbose: VerboseOpt = False,
    temperature: TemperatureOpt = None,
    top_p: TopPOpt = None,
    effort: EffortOpt = None,
    context_budget: ContextBudgetOpt = None,
) -> None:
    """Work toward a goal in rounds until a check command passes or the model reports it met, then show the diff."""
    from prax_bot.core.goal import GoalDriver, goal_task

    root = _repo_root(repo)
    settings = build_settings(
        model=model,
        base_image=base_image,
        setup=setup,
        max_steps=max_steps,
        temperature=temperature,
        top_p=top_p,
        effort=effort,
        context_budget=context_budget,
        backend=backend,
        repo_root=root,
    )
    backend = settings.backend
    _require_credentials(settings, need_project=backend == "contree")
    _header(
        settings,
        root,
        backend,
        f"\n[dim]goal[/]   up to {rounds} rounds"
        + (f", met when `{escape(check)}` exits 0" if check else ", the model decides when it is met"),
    )
    console.print(f"[bold]goal[/]  {escape(objective)}")
    skills, plugins = load_extensions(settings)
    console.print()

    run_id, run_dir, events_file, on_event = new_run(settings, verbose)
    started = time.monotonic()
    ws = open_workspace(settings, backend, root, run_id, on_event)
    driver = GoalDriver(objective, ws, max_rounds=rounds, check=check, check_timeout=settings.command_timeout)

    def after_round(round_no: int, result: Any, diff: str) -> str | None:
        nxt = driver.next_instruction(round_no, result, diff)
        on_event(
            "goal.round",
            round=round_no,
            met=driver.met,
            stopped=driver.stopped,
            check_passed=driver.met if check else None,
        )
        if nxt is None:
            console.print(f"\n[bold]{'[green]' if driver.met else '[yellow]'}{escape(driver.status)}[/][/]")
        return nxt

    _solve(
        settings,
        ws,
        root,
        goal_task(objective, rounds, check),
        backend=backend,
        run_id=run_id,
        run_dir=run_dir,
        events_file=events_file,
        on_event=on_event,
        yes=yes,
        export=export,
        protect=protect,
        started=started,
        record_task=objective,
        goal=True,
        auto_next=after_round,
        skills=skills,
        plugins=plugins,
        extra=lambda: {
            "goal": {
                "objective": objective,
                "check": check,
                "max_rounds": rounds,
                "rounds": driver.rounds,
                "met": driver.met,
                "stopped": driver.stopped,
            }
        },
    )


@app.command()
def resume(
    run_id: str | None = typer.Argument(None, help="The run to continue. Default: the latest run"),
    instruction: str | None = typer.Argument(None, help="What to do next. Default: finish the original task"),
    from_step: int | None = typer.Option(
        None, "--from", min=0, help="Checkpoint to start from (see praxbot inspect --list). Default: the last one"
    ),
    model: str | None = typer.Option(None, help="Model id on Token Factory. Default: the model of the run"),
    max_steps: MaxStepsOpt = None,
    yes: YesOpt = False,
    export: ExportOpt = None,
    protect: ProtectOpt = (),
    verbose: VerboseOpt = False,
    temperature: TemperatureOpt = None,
    top_p: TopPOpt = None,
    effort: EffortOpt = None,
    context_budget: ContextBudgetOpt = None,
) -> None:
    """Continue a past run from one of its checkpoints, in a workspace rebuilt from that image."""
    from contree_sdk.sdk.exceptions import ForbiddenError

    from prax_bot.runtime.runs import continuation_task, latest_run_id, load_run, reopen_workspace
    from prax_bot.workspace.sandbox import WorkspaceError

    settings = build_settings(
        model=model,
        max_steps=max_steps,
        temperature=temperature,
        top_p=top_p,
        effort=effort,
        context_budget=context_budget,
    )
    run_id = run_id or latest_run_id(settings)
    if run_id is None:
        console.print("no runs yet")
        raise typer.Exit(1)
    try:
        past = load_run(run_id, settings)
    except FileNotFoundError as e:
        console.print(f"[red]✗ {escape(str(e))}[/]")
        raise typer.Exit(1) from None
    info = past.info
    if not past.checkpoints:
        console.print(f"[red]✗ run {run_id} has no checkpoints to resume from[/]")
        raise typer.Exit(1)
    step = len(past.checkpoints) - 1 if from_step is None else from_step
    if not 0 <= step < len(past.checkpoints):
        console.print(f"[red]✗ run {run_id} has checkpoints 0 to {len(past.checkpoints) - 1}[/]")
        raise typer.Exit(1)
    backend = str(info.get("backend") or "contree")
    root = _repo_root(Path(str(info.get("repo") or ".")))
    settings = build_settings(
        model=model,
        max_steps=max_steps,
        temperature=temperature,
        top_p=top_p,
        effort=effort,
        context_budget=context_budget,
        backend=backend,
        repo_root=root,
    )
    if model is None and info.get("model"):
        settings = dataclasses.replace(settings, model=str(info["model"]))
    _require_credentials(settings, need_project=backend == "contree")
    checkpoint = past.checkpoints[step]

    console.print(
        Panel.fit(
            f"[bold]Prax Bot[/] {__version__}\n"
            f"[dim]repo[/]   {root}\n[dim]model[/]  {settings.model}\n"
            f"[dim]resume[/] {run_id} from checkpoint {step} ({escape(str(checkpoint.get('label', ''))[:60])}), image {escape(str(checkpoint['image']))}",
            border_style="cyan",
        )
    )
    console.print(f"[bold]task[/]  {escape(str(info.get('task', '')))}")
    console.print(f"[bold]next[/]  {escape(instruction or 'finish the original task')}")
    skills, plugins = load_extensions(settings)
    console.print()

    new_run_id, run_dir, events_file, on_event = new_run(settings, verbose)
    started = time.monotonic()
    console.print("[bold]sandbox[/]" if backend == "contree" else "[bold]local workspace[/]")
    try:
        ws = reopen_workspace(past, step, settings, on_event, new_run_id)
        diff_so_far = ws.diff()
    except (WorkspaceError, FileNotFoundError) as e:
        console.print(f"[red]✗ {escape(str(e))}[/]")
        raise typer.Exit(1) from None
    except ForbiddenError:
        console.print(f"[red]✗ {escape(NO_SANDBOX_ACCESS)}[/]")
        raise typer.Exit(1) from None
    except Exception as e:
        console.print(f"[red]✗ sandbox error: {type(e).__name__}: {escape(str(e))}[/]")
        raise typer.Exit(1) from None

    instruction = (
        instruction
        or "Continue the original task from this state: check what is still missing, finish it, and run the tests."
    )
    task = continuation_task(str(info.get("task", "")), str(info.get("summary") or ""), diff_so_far, instruction)
    _solve(
        settings,
        ws,
        root,
        task,
        backend=backend,
        run_id=new_run_id,
        run_dir=run_dir,
        events_file=events_file,
        on_event=on_event,
        yes=yes,
        export=export,
        protect=protect,
        started=started,
        resumed_from={"run_id": run_id, "step": step, "image": checkpoint["image"], "instruction": instruction},
        record_task=str(info.get("task", "")),
        skills=skills,
        plugins=plugins,
    )


@app.command()
def doctor(
    base_image: str | None = typer.Option(None, help="OCI image to test. Default: python:3.12"),
    skip_sandbox: bool = typer.Option(False, help="Skip the sandbox checks"),
) -> None:
    """Check credentials, the Nemotron model, tool calling, and the sandbox."""
    settings = build_settings(base_image=base_image, repo_root=_maybe_repo_root(Path(".")))
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

    console.print("\n[bold]config[/]")
    row(True, "files", ", ".join(str(f) for f in settings.config_files) or "none (prax config --init writes one)")
    for warning in settings.warnings:
        row(False, "warning", warning)

    console.print("\n[bold]token factory[/]")
    from prax_bot.core.llm import LLM

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
    except Exception as e:
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
    except Exception as e:
        row(False, "tool-call round trip", f"{type(e).__name__}: {e}")
        ok = False

    if not skip_sandbox:
        console.print("\n[bold]sandbox[/]")
        from contree_sdk.sdk.exceptions import ForbiddenError

        try:
            from prax_bot.workspace.sandbox import make_client

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
            row(
                isolated,
                "fork isolation",
                "two runs from one image cannot see each other's files" if isolated else "files leaked between forks",
            )
            ok &= isolated

            c = a.apply_files({"/tmp/c.txt": b"upload ok"})
            content = c.read("/tmp/c.txt")
            row(content == b"upload ok", "upload and read a file", f"{len(content)} bytes")
            ok &= content == b"upload ok"
        except ForbiddenError:
            row(False, "sandbox access", NO_SANDBOX_ACCESS)
            ok = False
        except Exception as e:
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
        root = _repo_root(repo)
        console.print(
            f"applying the state of checkpoint {step} ({escape(run.checkpoints[int(step)]['label'])}) to {root}"
        )
        if not apply_diff(root, text):
            raise typer.Exit(1)
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
    from prax_bot.runtime.render import describe_call

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


@app.command()
def report(
    run_id: str | None = typer.Argument(None, help="Run id; default: the latest run"),
    out: Path | None = typer.Option(None, help="Where to write the HTML; default: report.html in the run directory"),
    open_: bool = typer.Option(False, "--open", help="Open the report in a browser"),
) -> None:
    """Write a self-contained HTML report for a run."""
    from prax_bot.runtime.runs import latest_run_id, load_run, render_html

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
