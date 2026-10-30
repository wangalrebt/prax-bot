"""Terminal-facing setup helpers shared by the command line, TUI and web app."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape

from prax_bot.extensions.plugins import ExtraTool
from prax_bot.extensions.skills import Skill
from prax_bot.runtime.runs import Run
from prax_bot.runtime.session import Session
from prax_bot.settings import Settings
from prax_bot.workspace.types import EventFn, WorkspaceError, noop

console = Console()

NO_SANDBOX_ACCESS = (
    "the sandbox API refused the request (403). Sandboxes are in beta and access is granted per project: "
    "request it at https://tokenfactory.nebius.com/sandboxes/about, or run with --backend local meanwhile."
)


def build_settings(**kwargs: Any) -> Settings:
    """Settings from flags, environment, config files and defaults; a config file's complaints are shown once."""
    try:
        settings = Settings.from_env(**kwargs)
    except (
        TypeError,
        ValueError,
    ) as e:  # a bad value, or a value of the wrong type, in a config file or the environment
        raise typer.BadParameter(str(e)) from None
    for warning in settings.warnings:
        console.print(f"[yellow]warning:[/] {escape(warning)}")
    return settings


def load_extensions(settings: Settings) -> tuple[list[Any], list[Any]]:
    """Personal skills and plugins from the home directory, announced on the terminal."""
    from prax_bot.core.tools import TOOL_MODELS
    from prax_bot.extensions.plugins import load_plugins
    from prax_bot.extensions.skills import home_skills

    skills = home_skills(settings.home)
    plugins = load_plugins(
        settings.home,
        set(TOOL_MODELS) | {"load_skill"},
        lambda msg: console.print(f"[yellow]warning:[/] {escape(msg)}"),
    )
    if skills:
        console.print(
            f"[dim]skills[/]   {escape(', '.join(sk.name for sk in skills))} (from {settings.home / 'skills'})"
        )
    if plugins:
        console.print(
            f"[dim]plugins[/]  {escape(', '.join(t.name for t in plugins))} (from {settings.home / 'plugins'})"
        )
    return skills, plugins


def create_session(
    settings: Settings,
    root: Path,
    *,
    verbose: bool = False,
    out: Console | None = None,
    hook: EventFn = noop,
    protect: Sequence[str] = (),
    skills: Sequence[Skill] = (),
    plugins: Sequence[ExtraTool] = (),
    kind: str = "session",
    resume: tuple[Run, int] | None = None,
    continue_session: bool = False,
) -> Session:
    """Attach interface observers to the shared session factory."""
    from contree_sdk.sdk.exceptions import ForbiddenError

    from prax_bot.runtime.factory import create_session as create
    from prax_bot.ui.render import Reporter

    output = out or console
    reporter = Reporter(output, verbose=verbose)

    def observe(kind: str, **payload: Any) -> None:
        reporter(kind, **payload)
        hook(kind, **payload)

    output.print("[bold]sandbox[/]" if settings.backend == "contree" else "[bold]local workspace[/]")
    try:
        return create(
            settings,
            root,
            on_event=observe,
            protect=protect,
            skills=skills,
            plugins=plugins,
            kind=kind,
            resume=resume,
            continue_session=continue_session,
        )
    except Exception as error:
        message = NO_SANDBOX_ACCESS if isinstance(error, ForbiddenError) else f"{type(error).__name__}: {error}"
        if out is not None:
            raise WorkspaceError(message) from error
        output.print(f"[red]✗ {escape(message)}[/]")
        raise typer.Exit(1) from error


def repo_root(path: Path) -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise typer.BadParameter(f"{path} is not inside a git repository") from None
    return Path(out)


def maybe_repo_root(path: Path) -> Path | None:
    """The repository root, or None when not inside one; for commands that work either way."""
    try:
        return repo_root(path)
    except typer.BadParameter:
        return None
