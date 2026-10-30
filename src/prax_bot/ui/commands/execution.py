"""Commands that open and run sessions."""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm

from prax_bot import __version__
from prax_bot.core.prompts import prompt
from prax_bot.runtime.session import Session
from prax_bot.settings import DEFAULT_MODEL, Settings
from prax_bot.ui.common import (
    build_settings,
    console,
    create_session,
    load_extensions,
    repo_root,
)
from prax_bot.ui.render import sampling_label
from prax_bot.ui.session import session_loop
from prax_bot.workspace.types import WorkspaceError

RepoOpt = Annotated[Path, typer.Option("--repo", "-C", help="A path inside the git repository to work on")]


ModelOpt = Annotated[str | None, typer.Option(help=f"Model id on Token Factory. Default: {DEFAULT_MODEL}")]


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
    list[str] | None, typer.Option("--protect", help="Glob the model may not edit, e.g. 'tests/*'. Repeatable")
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


TestCommandOpt = Annotated[
    str | None, typer.Option("--test-command", help="Test command run independently after each round")
]


LintCommandOpt = Annotated[
    str | None, typer.Option("--lint-command", help="Lint command run independently after each round")
]


EffortOpt = Annotated[
    str | None, typer.Option(help="How much the model reasons per step: off, low (default), high, or xhigh")
]


INIT_TASK = prompt("init_task")


@dataclass
class WorkspaceOptions:
    """The options every command that opens a workspace accepts, resolved the same way each time."""

    repo: Path
    model: str | None = None
    base_image: str | None = None
    setup: str | None = None
    max_steps: int | None = None
    backend: str | None = None
    protect: Sequence[str] | None = None
    temperature: float | None = None
    top_p: float | None = None
    effort: str | None = None
    context_budget: int | None = None
    test_command: str | None = None
    lint_command: str | None = None
    investigate_model: str | None = None

    def resolve(self) -> tuple[Path, Settings]:
        """The repository root and the effective settings; exits when credentials are missing."""
        root = repo_root(self.repo)
        settings = build_settings(
            model=self.model,
            investigate_model=self.investigate_model,
            base_image=self.base_image,
            setup=self.setup,
            max_steps=self.max_steps,
            temperature=self.temperature,
            top_p=self.top_p,
            effort=self.effort,
            context_budget=self.context_budget,
            backend=self.backend,
            repo_root=root,
            test_command=self.test_command,
            lint_command=self.lint_command,
        )
        _require_credentials(settings, need_project=settings.backend == "contree")
        return root, settings

    @property
    def protected(self) -> list[str]:
        return list(self.protect or ())


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
    session: Session,
    task: str,
    *,
    yes: bool,
    export: Path | None,
    record_task: str | None = None,
    ask_followup: Any = None,
    objective: str | None = None,
    check: str | None = None,
    rounds: int = 5,
) -> None:
    """Drive the shared runtime, collecting terminal follow-ups and the apply decision."""
    ask_followup = ask_followup or (_ask_followup if sys.stdin.isatty() and not yes else lambda: "")
    try:
        if objective is not None:
            driver = session.run_goal(objective, max_rounds=rounds, check=check)
            console.print(escape(driver.status))
        elif session.resumed_from:
            session.instruct(task)
        else:
            session.run_round(task, original_task=record_task)
        while session.last_result and session.last_result.reason not in ("error", "cancelled"):
            more = ask_followup()
            if not more:
                break
            session.instruct(more)
        diff = session.diff()
        # Applying advances the baseline image; judge this run against the image it verified.
        verification = session.verification_status
        if export is not None:
            export.write_text(diff, encoding="utf-8")
            console.print(f"diff written to {export}")
        if session.last_result and session.last_result.reason == "cancelled":
            console.print(f"stopped; pending changes saved at {session.run_dir / 'diff.patch'}")
            raise typer.Exit(130)
        if diff.strip():
            apply = yes or (sys.stdin.isatty() and Confirm.ask("Apply this diff to the working tree?", default=False))
            if apply:
                session.apply()
            else:
                console.print(f"pending changes saved at {session.run_dir / 'diff.patch'}")
        if session.last_result and session.last_result.reason == "error":
            raise typer.Exit(1)
        if verification not in ("passed", "not configured") or (session.goal_info and not session.goal_info["met"]):
            raise typer.Exit(1)
    except WorkspaceError as error:
        console.print(f"[red]✗ {escape(str(error))}[/]")
        raise typer.Exit(1) from None
    finally:
        try:
            session.close()
        except WorkspaceError as error:
            console.print(f"[red]✗ {escape(str(error))}[/]")


def session(
    repo: RepoOpt = Path("."),
    model: ModelOpt = None,
    base_image: BaseImageOpt = None,
    setup: SetupOpt = None,
    max_steps: MaxStepsOpt = None,
    backend: BackendOpt = None,
    protect: ProtectOpt = None,
    verbose: VerboseOpt = False,
    temperature: TemperatureOpt = None,
    top_p: TopPOpt = None,
    effort: EffortOpt = None,
    context_budget: ContextBudgetOpt = None,
    test_command: TestCommandOpt = None,
    lint_command: LintCommandOpt = None,
) -> None:
    """Open a plain terminal session and keep giving it instructions."""
    opts = WorkspaceOptions(
        repo,
        model,
        base_image,
        setup,
        max_steps,
        backend,
        protect,
        temperature,
        top_p,
        effort,
        context_budget,
        test_command,
        lint_command,
    )
    root, settings = opts.resolve()
    _header(settings, root, settings.backend, "\n[dim]mode[/]   session: one sandbox, many instructions")
    skills, plugins = load_extensions(settings)
    session = create_session(settings, root, verbose=verbose, protect=opts.protected, skills=skills, plugins=plugins)
    session_loop(session, console)


def ui(
    repo: RepoOpt = Path("."),
    model: ModelOpt = None,
    base_image: BaseImageOpt = None,
    setup: SetupOpt = None,
    max_steps: MaxStepsOpt = None,
    backend: BackendOpt = None,
    protect: ProtectOpt = None,
    verbose: VerboseOpt = False,
    temperature: TemperatureOpt = None,
    top_p: TopPOpt = None,
    effort: EffortOpt = None,
    context_budget: ContextBudgetOpt = None,
    test_command: TestCommandOpt = None,
    lint_command: LintCommandOpt = None,
) -> None:
    """The full-screen terminal interface: conversation, sidebar, diff pane, one input line."""
    from prax_bot.ui.tui import run_ui

    opts = WorkspaceOptions(
        repo,
        model,
        base_image,
        setup,
        max_steps,
        backend,
        protect,
        temperature,
        top_p,
        effort,
        context_budget,
        test_command,
        lint_command,
    )
    root, settings = opts.resolve()
    skills, plugins = load_extensions(settings)
    run_ui(settings, root, settings.backend, opts.protected, skills, plugins, verbose)


def web(
    repo: RepoOpt = Path("."),
    port: int = typer.Option(0, "--port", min=0, max=65535, help="Port on localhost; 0 picks a free one"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the URL instead of opening a browser"),
    model: ModelOpt = None,
    base_image: BaseImageOpt = None,
    setup: SetupOpt = None,
    max_steps: MaxStepsOpt = None,
    backend: BackendOpt = None,
    protect: ProtectOpt = None,
    verbose: VerboseOpt = False,
    temperature: TemperatureOpt = None,
    top_p: TopPOpt = None,
    effort: EffortOpt = None,
    context_budget: ContextBudgetOpt = None,
    test_command: TestCommandOpt = None,
    lint_command: LintCommandOpt = None,
) -> None:
    """The web interface on localhost: chat with tool calls, changes, to-dos and rounds in a sidebar, a slash-command menu."""
    opts = WorkspaceOptions(
        repo,
        model,
        base_image,
        setup,
        max_steps,
        backend,
        protect,
        temperature,
        top_p,
        effort,
        context_budget,
        test_command,
        lint_command,
    )
    root, settings = opts.resolve()
    _start_web(settings, root, settings.backend, opts.protected, verbose, port, not no_browser)


def _start_web(
    settings: Settings, root: Path, backend: str, protect: list[str], verbose: bool, port: int, open_browser: bool
) -> None:
    from prax_bot.ui.web import serve
    from prax_bot.ui.web_sessions import WebSessions

    skills, plugins = load_extensions(settings)

    def make_session(out: Console, hook: Any, past: Any, extra_tools: Any) -> Session:
        return create_session(
            settings,
            root,
            verbose=verbose,
            out=out,
            hook=hook,
            protect=protect,
            skills=skills,
            plugins=[*plugins, *extra_tools],
            resume=(past, len(past.checkpoints) - 1) if past is not None else None,
            continue_session=past is not None,
        )

    serve(WebSessions(settings, root, backend, make_session), port=port, open_browser=open_browser)


def main(ctx: typer.Context) -> None:
    """Prax Bot: a coding agent that works in a Nebius Token Factory Sandbox, powered by NVIDIA Nemotron. With no command, opens the web interface."""
    if ctx.invoked_subcommand is None:
        root = repo_root(Path("."))
        settings = build_settings(repo_root=root)
        _require_credentials(settings, need_project=settings.backend == "contree")
        _start_web(settings, root, settings.backend, [], False, 0, True)


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
    protect: ProtectOpt = None,
    verbose: VerboseOpt = False,
    temperature: TemperatureOpt = None,
    top_p: TopPOpt = None,
    effort: EffortOpt = None,
    context_budget: ContextBudgetOpt = None,
    test_command: TestCommandOpt = None,
    lint_command: LintCommandOpt = None,
) -> None:
    """Solve a task in the current repository. Runs entirely in a sandbox, shows the diff, takes follow-up instructions, then asks before applying."""
    opts = WorkspaceOptions(
        repo,
        model,
        base_image,
        setup,
        max_steps,
        backend,
        protect,
        temperature,
        top_p,
        effort,
        context_budget,
        test_command,
        lint_command,
        investigate_model,
    )
    root, settings = opts.resolve()
    _header(settings, root, settings.backend)
    console.print(f"[bold]task[/]  {escape(task)}")
    skills, plugins = load_extensions(settings)
    console.print()
    session = create_session(
        settings, root, verbose=verbose, protect=opts.protected, skills=skills, plugins=plugins, kind="run"
    )
    _solve(session, task, yes=yes, export=export)


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
    root, settings = WorkspaceOptions(repo, model, base_image, setup, max_steps, backend).resolve()
    if (root / "AGENTS.md").exists() and not force:
        console.print(f"{root / 'AGENTS.md'} already exists; pass --force to replace it, or edit it by hand.")
        raise typer.Exit(1)
    task = INIT_TASK.format(
        what="Replace the existing AGENTS.md at the repository root"
        if force
        else "Create AGENTS.md at the repository root"
    )
    _header(settings, root, settings.backend)
    console.print("[bold]task[/]  write AGENTS.md")
    skills, plugins = load_extensions(settings)
    console.print()
    session = create_session(settings, root, verbose=verbose, skills=skills, plugins=plugins, kind="run")
    _solve(session, task, yes=yes, export=None, record_task="init: write AGENTS.md")


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
    protect: ProtectOpt = None,
    verbose: VerboseOpt = False,
    temperature: TemperatureOpt = None,
    top_p: TopPOpt = None,
    effort: EffortOpt = None,
    context_budget: ContextBudgetOpt = None,
    test_command: TestCommandOpt = None,
    lint_command: LintCommandOpt = None,
) -> None:
    """Work toward a goal in rounds until a check command passes or the model reports it met, then show the diff."""
    opts = WorkspaceOptions(
        repo,
        model,
        base_image,
        setup,
        max_steps,
        backend,
        protect,
        temperature,
        top_p,
        effort,
        context_budget,
        test_command,
        lint_command,
    )
    root, settings = opts.resolve()
    _header(
        settings,
        root,
        settings.backend,
        f"\n[dim]goal[/]   up to {rounds} rounds"
        + (f", met when `{escape(check)}` exits 0" if check else ", the model decides when it is met"),
    )
    console.print(f"[bold]goal[/]  {escape(objective)}")
    skills, plugins = load_extensions(settings)
    console.print()

    session = create_session(
        settings, root, verbose=verbose, protect=opts.protected, skills=skills, plugins=plugins, kind="goal"
    )
    _solve(session, objective, yes=yes, export=export, objective=objective, check=check, rounds=rounds)


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
    protect: ProtectOpt = None,
    verbose: VerboseOpt = False,
    temperature: TemperatureOpt = None,
    top_p: TopPOpt = None,
    effort: EffortOpt = None,
    context_budget: ContextBudgetOpt = None,
    test_command: TestCommandOpt = None,
    lint_command: LintCommandOpt = None,
) -> None:
    """Continue a past run from one of its checkpoints, in a workspace rebuilt from that image."""

    from prax_bot.runtime.runs import latest_run_id, load_run

    settings = build_settings(
        model=model,
        max_steps=max_steps,
        temperature=temperature,
        top_p=top_p,
        effort=effort,
        context_budget=context_budget,
        test_command=test_command,
        lint_command=lint_command,
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
    root = repo_root(Path(str(info.get("repo") or ".")))
    settings = build_settings(
        model=model,
        max_steps=max_steps,
        temperature=temperature,
        top_p=top_p,
        effort=effort,
        context_budget=context_budget,
        backend=backend,
        repo_root=root,
        test_command=test_command,
        lint_command=lint_command,
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

    session = create_session(
        settings,
        root,
        verbose=verbose,
        protect=list(protect or ()),
        skills=skills,
        plugins=plugins,
        kind="resume",
        resume=(past, step),
    )
    _solve(
        session,
        instruction
        or "Continue the original task from this state: check what is still missing, finish it, and run the tests.",
        yes=yes,
        export=export,
    )
