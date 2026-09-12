"""Configuration, prompt inspection, and setup diagnostics."""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer
from rich.markup import escape
from rich.table import Table

from prax_bot import __version__
from prax_bot.settings import contree_auth_ini
from prax_bot.ui.common import (
    NO_SANDBOX_ACCESS,
    build_settings,
    console,
    maybe_repo_root,
)

STARTER_USER_CONFIG = """# Prax Bot settings. A flag or a PRAXBOT_* environment variable overrides anything here.
# model = "nvidia/nemotron-3-super-120b-a12b"
# effort = "low"            # off, low, high, xhigh
# backend = "contree"       # contree: a Token Factory sandbox; local: this machine, for development
# base_image = "python:3.12"
# max_steps = 40
# test_command = "python -m pytest -q"
# lint_command = "ruff check ."
# temperature = 1.0
# top_p = 0.95
"""


STARTER_REPO_CONFIG = """# Prax Bot settings for this repository; they override ~/.praxbot/config.toml.
# A repository cannot choose the backend, and its setup command only runs inside a sandbox.
# setup = "pip install -e . pytest"   # runs once after the upload, before the agent starts
# base_image = "python:3.12"
# max_steps = 40
# test_command = "python -m pytest -q"
# lint_command = "ruff check ."
# effort = "low"
"""


def config(
    init: bool = typer.Option(False, "--init", help="Write a starter ~/.praxbot/config.toml"),
    init_repo: bool = typer.Option(
        False, "--init-repo", help="Write a starter .praxbot/config.toml in this repository"
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file"),
) -> None:
    """Show the effective settings and where each one comes from; --init writes a starter file to edit."""
    from prax_bot.settings import CONFIG_FILE, REPO_CONFIG_FILE

    root = maybe_repo_root(Path("."))
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


def prompt_command(
    name: str = typer.Argument(
        "system", help="Which prompt: system, summary, nudge, continuation, goal, protected, tools/run, ..."
    ),
    path: bool = typer.Option(False, "--path", help="Print where the prompt comes from instead of its text"),
    list_all: bool = typer.Option(False, "--list", help="List every prompt and where each comes from"),
) -> None:
    """Print a prompt template; defaults to the core system rules, before runtime context is appended."""
    from prax_bot.core import prompts

    settings = build_settings(repo_root=maybe_repo_root(Path(".")))
    if list_all:
        table = Table(box=None, padding=(0, 2), header_style="dim")
        for column in ("prompt", "from", "file"):
            table.add_column(column)
        for n in prompts.names():
            kind, where = prompts.source(n)
            if n == "system" and settings.system_prompt_file:
                kind, where = "system_prompt_file", Path(settings.system_prompt_file).expanduser()
            table.add_row(n, kind, str(where))
        console.print(table)
        console.print(f"[dim]override any of them with a file of the same name under {prompts.override_dir()}[/]")
        return
    try:
        kind, where = prompts.source(name)
    except KeyError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(1) from None
    custom = settings.system_prompt_text() if name == "system" else None
    if custom:
        kind, where = "system_prompt_file", Path(settings.system_prompt_file).expanduser()
    if path:
        console.print(f"{kind}: {where}")
        console.print("[dim]project rules belong in AGENTS.md; prax prompt --list shows every prompt[/]")
        return
    print(custom or prompts.text(name))


def doctor(
    base_image: str | None = typer.Option(None, help="OCI image to test. Default: python:3.12"),
    skip_sandbox: bool = typer.Option(False, help="Skip the sandbox checks"),
) -> None:
    """Check credentials, the Nemotron model, tool calling, and the sandbox."""
    settings = build_settings(base_image=base_image, repo_root=maybe_repo_root(Path(".")))
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


def version() -> None:
    """Print the version."""
    console.print(f"praxbot {__version__}")
