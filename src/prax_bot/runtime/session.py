"""The session runtime shared by every front end: one workspace, rounds of agent work, records, and the shell helpers.

`praxbot run`, the plain session, the terminal interface and the web interface all drive the same pieces:
open a workspace, run the agent for a round, show the result and the diff, apply or undo, and write the
run record. Nothing here knows which front end is calling.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm
from rich.syntax import Syntax
from rich.table import Table

from prax_bot.settings import Settings

QUIT_COMMANDS = ("/quit", "/exit", "/q")


MAX_DIFF_LINES_SHOWN = 400


NO_SANDBOX_ACCESS = (
    "the sandbox API refused the request (403). Sandboxes are in beta and access is granted per project: "
    "request it at https://tokenfactory.nebius.com/sandboxes/about, or run with --backend local meanwhile."
)


console = Console()


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


def sampling_label(settings: Settings) -> str:
    parts = [f"temperature {settings.temperature:g}"]
    if settings.top_p is not None:
        parts.append(f"top_p {settings.top_p:g}")
    parts.append(f"effort {settings.effort}")
    return " · ".join(parts)


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


def apply_diff(root: Path, diff: str, out: Console | None = None) -> bool:
    out = out or console
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as f:
        f.write(diff)
        patch_path = f.name
    try:
        check = subprocess.run(
            ["git", "apply", "--check", patch_path], cwd=root, capture_output=True, text=True, check=False
        )
        if check.returncode != 0:
            out.print(f"[red]the diff does not apply cleanly:[/] {escape(check.stderr.strip())}")
            return False
        applied = subprocess.run(["git", "apply", patch_path], cwd=root, capture_output=True, text=True, check=False)
        if applied.returncode != 0:
            out.print(f"[red]git apply failed:[/] {escape(applied.stderr.strip())}")
            return False
    finally:
        os.unlink(patch_path)
    status = subprocess.run(["git", "status", "--short"], cwd=root, capture_output=True, text=True, check=False).stdout
    out.print("[green]applied to the working tree[/] (nothing committed)")
    if status.strip():
        out.print(escape(status.rstrip()))
    return True


def new_run(settings: Settings, verbose: bool, out: Console | None = None) -> tuple[str, Path, Any, Any]:
    """A fresh run directory with its event log and the on_event callback that feeds it and the terminal."""
    from prax_bot.runtime.render import Reporter

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    run_dir = settings.home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    events_file = (run_dir / "events.jsonl").open("a", encoding="utf-8")
    reporter = Reporter(out or console, verbose=verbose)

    def on_event(kind: str, **payload: Any) -> None:
        record = {"t": time.time(), "kind": kind, **{k: json_safe(v) for k, v in payload.items()}}
        events_file.write(json.dumps(record) + "\n")
        events_file.flush()
        reporter(kind, **payload)

    return run_id, run_dir, events_file, on_event


def open_workspace(
    settings: Settings, backend: str, root: Path, run_id: str, on_event: Any, out: Console | None = None
) -> Any:
    from contree_sdk.sdk.exceptions import ForbiddenError

    from prax_bot.workspace.sandbox import WorkspaceError, create_workspace, make_client

    interface = out is not None
    out = out or console
    if not interface:
        out.print("[bold]sandbox[/]" if backend == "contree" else "[bold]local workspace[/]")
    try:
        if backend == "local":
            from prax_bot.workspace.local import create_local_workspace

            return create_local_workspace(settings, root, on_event, run_id)
        client = make_client()
        return create_workspace(client, settings, root, on_event)
    except WorkspaceError as e:
        out.print(f"[red]✗ {escape(str(e))}[/]")
    except ForbiddenError:
        out.print(f"[red]✗ {escape(NO_SANDBOX_ACCESS)}[/]")
    except Exception as e:
        out.print(f"[red]✗ sandbox error: {type(e).__name__}: {escape(str(e))}[/]")
    if interface:
        raise RuntimeError("the workspace could not be created")
    raise typer.Exit(1)


def agent_round(
    settings: Settings,
    ws: Any,
    task: str,
    llm: Any,
    on_event: Any,
    *,
    protect: list[str],
    skills: Any = (),
    plugins: Any = (),
    goal: bool = False,
    out: Console | None = None,
) -> Any:
    """One agent transcript on the workspace. Interruptions and errors keep the work already in the workspace."""
    from prax_bot.core.agent import Agent, AgentResult
    from prax_bot.core.tools import ToolBox

    out = out or console
    out.print("\n[bold]agent[/]")
    agent = Agent(
        llm,
        ToolBox(ws, protected=protect, skills=skills, plugins=plugins),
        max_steps=settings.max_steps,
        on_event=on_event,
        investigate_model=settings.investigate_model,
        investigate_steps=settings.investigate_steps,
        goal=goal,
    )
    try:
        return agent.run(task)
    except KeyboardInterrupt:
        out.print("\n[yellow]interrupted; collecting the diff so far[/]")
        return AgentResult(False, "interrupted by the user", 0, "interrupted")
    except Exception as e:
        out.print(f"\n[red]✗ the agent stopped: {type(e).__name__}: {escape(str(e)[:400])}[/]")
        return AgentResult(False, f"stopped by an error: {type(e).__name__}: {str(e)[:200]}", 0, "error")


def print_result(
    result: Any,
    llm: Any,
    ws: Any,
    run_dir: Path,
    *,
    steps_total: int,
    round_steps: int | None,
    elapsed: float,
    out: Console | None = None,
) -> None:
    out = out or console
    out.print()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("result", "[green]finished[/]" if result.finished else f"[yellow]stopped: {result.reason}[/]")
    table.add_row("steps", str(steps_total) if round_steps is None else f"{steps_total} ({round_steps} in this round)")
    table.add_row("tokens", f"{llm.usage.total_tokens:,} in {llm.usage.calls} calls")
    table.add_row("sandbox", f"{ws.commands} commands · {len(ws.checkpoints)} checkpoints · cost {ws.total_cost:.4f}")
    table.add_row("images", f"baseline {ws.checkpoints[0].image} → final {ws.image_id}")
    table.add_row("elapsed", f"{elapsed:.0f}s")
    table.add_row("log", str(run_dir))
    out.print(table)
    out.print(
        Panel(
            escape(result.summary or "(no summary)"),
            title="summary",
            border_style="green" if result.finished else "yellow",
        )
    )


def print_diff(diff: str, ws: Any, run_dir: Path, export: Path | None = None, out: Console | None = None) -> None:
    out = out or console
    if ws.dropped_scratch:
        out.print(
            f"[dim]left out of the diff, they look like scratch files: {escape(', '.join(ws.dropped_scratch))}[/]"
        )
    if not diff.strip():
        out.print("no changes to the repository")
        return
    lines = diff.splitlines()
    out.print(Syntax("\n".join(lines[:MAX_DIFF_LINES_SHOWN]), "diff", theme="ansi_dark", word_wrap=False))
    if len(lines) > MAX_DIFF_LINES_SHOWN:
        out.print(f"[dim]… {len(lines) - MAX_DIFF_LINES_SHOWN} more lines in {run_dir / 'diff.patch'}[/]")
    if export:
        export.write_text(diff, encoding="utf-8")
        out.print(f"diff written to {export}")


SESSION_HELP = """Type an instruction to have the agent work on it in the sandbox. Each instruction continues from the current state.
  /diff                 show the changes so far
  /apply                apply the changes to your working tree; later diffs continue from there
  /undo                 put the sandbox back to where it was before the last instruction
  /goal [--check CMD] [--rounds N] OBJECTIVE   work in rounds until the check passes or the model reports the goal met
  /resume RUN [--from N]   swap in a workspace rebuilt from a past run's checkpoint
  /effort LEVEL         off, low, high or xhigh for the next instructions
  /status               steps, tokens, checkpoints, and what is not applied yet
  /help                 this text
  /quit                 leave; you are asked about unapplied changes"""


def build_record(
    *,
    run_id: str,
    task: str,
    followups: list[str],
    repo: Path,
    settings: Settings,
    llm: Any,
    ws: Any,
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


def write_record(run_dir: Path, record: dict[str, Any]) -> None:
    (run_dir / "run.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


class Session:
    """One sandbox, many instructions: the interactive form of praxbot run."""

    def __init__(
        self,
        settings: Settings,
        ws: Any,
        root: Path,
        *,
        backend: str,
        run_id: str,
        run_dir: Path,
        events_file: Any,
        on_event: Any,
        protect: list[str],
        skills: Any = (),
        plugins: Any = (),
        read_line: Any = None,
        out: Console | None = None,
    ) -> None:
        from prax_bot.core.llm import LLM

        self.out = out or console
        self.settings, self.ws, self.root, self.backend = settings, ws, root, backend
        self.run_id, self.run_dir, self.events_file, self.on_event = run_id, run_dir, events_file, on_event
        self.protect, self.skills, self.plugins = protect, skills, plugins
        self.read_line = read_line or self._prompt
        self.llm = LLM(settings)
        self.started = time.monotonic()
        self.first_task: str | None = None
        self.instructions: list[str] = []
        self.last_result: Any = None
        self.last_summary = ""
        self.steps = 0
        self.rounds = 0
        self.round_starts: list[int] = []  # checkpoint before each instruction, for /undo
        self.applied_rounds = 0
        self.last_diff = ""

    @staticmethod
    def _prompt() -> str | None:
        from rich.prompt import Prompt

        try:
            return Prompt.ask("\n[bold cyan]praxbot[/]", default="", show_default=False)
        except (KeyboardInterrupt, EOFError):
            console.print()
            return None

    def diff(self) -> str:
        try:
            self.last_diff = self.ws.diff()
            (self.run_dir / "diff.patch").write_text(self.last_diff, encoding="utf-8")
        except Exception as e:
            self.out.print(f"[red]✗ could not extract the diff: {type(e).__name__}: {escape(str(e)[:300])}[/]")
        return self.last_diff

    def record(self) -> None:
        r = self.last_result
        write_record(
            self.run_dir,
            build_record(
                run_id=self.run_id,
                task=self.first_task or "(session)",
                followups=self.instructions,
                repo=self.root,
                settings=self.settings,
                llm=self.llm,
                ws=self.ws,
                backend=self.backend,
                base_image=self.settings.base_image if self.backend == "contree" else None,
                result=r,
                steps=self.steps,
                summary=self.last_summary,
                elapsed_s=time.monotonic() - self.started,
                applied=self.applied_rounds > 0,
                extra={"session": {"rounds": self.rounds, "applied_rounds": self.applied_rounds}},
            ),
        )

    def run_round(self, task: str, *, goal: bool = False) -> tuple[Any, str]:
        self.round_starts.append(self.ws.current_step)
        result = agent_round(
            self.settings,
            self.ws,
            task,
            self.llm,
            self.on_event,
            protect=self.protect,
            skills=self.skills,
            plugins=self.plugins,
            goal=goal,
            out=self.out,
        )
        self.steps += result.steps
        self.rounds += 1
        self.last_result, self.last_summary = result, result.summary
        diff = self.diff()
        self.record()
        print_result(
            result,
            self.llm,
            self.ws,
            self.run_dir,
            steps_total=self.steps,
            round_steps=result.steps if self.rounds > 1 else None,
            elapsed=time.monotonic() - self.started,
            out=self.out,
        )
        print_diff(diff, self.ws, self.run_dir, out=self.out)
        return result, diff

    def instruct(self, text: str) -> None:
        from prax_bot.runtime.runs import continuation_task

        self.on_event("instruction", text=text)
        if self.first_task is None:
            self.first_task = text
            task = text
        else:
            self.instructions.append(text)
            task = continuation_task(self.first_task, self.last_summary, self.diff(), text)
        self.run_round(task)

    def goal(self, argument: str) -> None:
        import shlex

        from prax_bot.core.goal import GoalDriver, goal_task
        from prax_bot.runtime.runs import continuation_task

        try:
            words = shlex.split(argument)
        except ValueError as e:
            self.out.print(f"[red]could not parse that: {escape(str(e))}[/]")
            return
        check: str | None = None
        rounds = 5
        rest: list[str] = []
        i = 0
        while i < len(words):
            if words[i] == "--check" and i + 1 < len(words):
                check, i = words[i + 1], i + 2
            elif words[i] == "--rounds" and i + 1 < len(words) and words[i + 1].isdigit():
                rounds, i = max(1, int(words[i + 1])), i + 2
            else:
                rest.append(words[i])
                i += 1
        objective = " ".join(rest).strip()
        if not objective:
            self.out.print("usage: /goal [--check CMD] [--rounds N] OBJECTIVE")
            return
        self.on_event("instruction", text=f"/goal {argument}")
        if self.first_task is None:
            self.first_task = objective
        else:
            self.instructions.append(f"goal: {objective}")
        driver = GoalDriver(
            objective, self.ws, max_rounds=rounds, check=check, check_timeout=self.settings.command_timeout
        )
        task = goal_task(objective, rounds, check)
        if self.rounds:
            task = continuation_task(self.first_task, self.last_summary, self.diff(), task)
        for n in range(1, rounds + 1):
            result, diff = self.run_round(task, goal=True)
            nxt = driver.next_instruction(n, result, diff)
            self.on_event("goal.round", round=n, met=driver.met, stopped=driver.stopped)
            if nxt is None:
                self.out.print(f"\n[bold]{'[green]' if driver.met else '[yellow]'}{escape(driver.status)}[/][/]")
                return
            task = continuation_task(objective, result.summary, diff, nxt)

    def apply(self) -> bool:
        diff = self.diff()
        if not diff.strip():
            self.out.print("nothing to apply")
            return True
        if not apply_diff(self.root, diff, self.out):
            self.out.print(f"the diff is saved at {self.run_dir / 'diff.patch'}")
            return False
        r = self.ws.run(
            "git add -A && git -c user.email=praxbot@localhost -c user.name=praxbot commit -qm 'praxbot session: applied to the working tree'",
            timeout=120,
            mutate=True,
        )
        if r.exit_code != 0:
            self.out.print(
                f"[yellow]applied, but the sandbox could not record it: {escape((r.stderr or r.stdout)[-300:])}[/]"
            )
        else:
            self.out.print("[dim]later diffs continue from this state[/]")
        self.applied_rounds = self.rounds
        self.round_starts.clear()  # /undo cannot reach behind an apply
        self.record()
        return True

    def undo(self) -> None:
        if not self.round_starts:
            self.out.print("nothing to undo")
            return
        step = self.round_starts.pop()
        cp = self.ws.restore(step)
        self.rounds = max(self.rounds - 1, 0)
        if self.instructions:
            self.instructions.pop()
        elif self.first_task is not None and self.rounds == 0:
            self.first_task = None
        self.out.print(f"restored the sandbox to checkpoint {cp.step} ({escape(cp.label)})")
        print_diff(self.diff(), self.ws, self.run_dir, out=self.out)
        self.record()

    def resume(self, argument: str) -> None:
        from prax_bot.runtime.runs import load_run, reopen_workspace

        words = argument.split()
        if not words:
            self.out.print("usage: /resume RUN-ID [--from N]")
            return
        run_id = words[0]
        step: int | None = None
        if "--from" in words:
            try:
                step = int(words[words.index("--from") + 1])
            except (IndexError, ValueError):
                self.out.print("usage: /resume RUN-ID [--from N]")
                return
        try:
            past = load_run(run_id, self.settings)
            if not past.checkpoints:
                raise ValueError(f"run {run_id} has no checkpoints")
            step = len(past.checkpoints) - 1 if step is None else step
            self.ws = reopen_workspace(past, step, self.settings, self.on_event, f"{self.run_id}-r{self.rounds + 1}")
        except Exception as e:
            self.out.print(f"[red]✗ could not resume: {type(e).__name__}: {escape(str(e)[:300])}[/]")
            return
        self.first_task = str(past.info.get("task") or "") or self.first_task
        self.last_summary = str(past.info.get("summary") or "")
        self.round_starts.clear()
        self.on_event("instruction", text=f"/resume {argument}")
        self.out.print(f"workspace rebuilt from {run_id} checkpoint {step}")
        print_diff(self.diff(), self.ws, self.run_dir, out=self.out)

    def status(self) -> None:
        diff = self.diff()
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_row("model", f"{self.settings.model} · {sampling_label(self.settings)}")
        table.add_row("rounds", f"{self.rounds} · {self.steps} steps · {self.llm.usage.total_tokens:,} tokens")
        table.add_row(
            "sandbox",
            f"{self.ws.commands} commands · {len(self.ws.checkpoints)} checkpoints · cost {self.ws.total_cost:.4f} · now {self.ws.image_id}",
        )
        table.add_row("changes", f"{diff.count('diff --git ')} files not applied" if diff.strip() else "none pending")
        table.add_row("log", str(self.run_dir))
        self.out.print(table)

    def loop(self) -> None:
        self.out.print("[dim]type an instruction, or /help[/]")
        while True:
            line = self.read_line()
            if line is None or not self.handle_line(line):
                break
        self.close()

    def handle_line(self, line: str) -> bool:
        """Act on one typed line; False means the session should end."""
        line = line.strip()
        if not line:
            return True
        if not line.startswith("/"):
            self.instruct(line)
            return True
        command, _, argument = line.partition(" ")
        command = command.lower()
        if command in QUIT_COMMANDS:
            return False
        handler = self.COMMANDS.get(command)
        if handler is None:
            self.out.print(f"unknown command {escape(command)}; /help lists them")
        else:
            handler(self, argument)
        return True

    def show_help(self, _argument: str = "") -> None:
        self.out.print(SESSION_HELP)

    def show_diff(self, _argument: str = "") -> None:
        print_diff(self.diff(), self.ws, self.run_dir, out=self.out)

    def set_effort(self, argument: str) -> None:
        try:
            self.settings = build_settings(
                model=self.settings.model,
                base_image=self.settings.base_image,
                setup=self.settings.setup,
                max_steps=self.settings.max_steps,
                temperature=self.settings.temperature,
                top_p=self.settings.top_p,
                effort=argument.strip() or None,
            )
        except typer.BadParameter as e:
            self.out.print(f"[red]{escape(str(e))}[/]")
            return
        from prax_bot.core.llm import LLM

        usage = self.llm.usage
        self.llm = LLM(self.settings)
        self.llm.usage = usage
        self.out.print(f"effort {self.settings.effort}")

    # slash commands the session understands; each handler takes the text after the command.
    # Defined after the methods it names, since a class body binds them in order.
    COMMANDS: ClassVar[dict[str, Callable[[Session, str], None]]] = {
        "/help": show_help,
        "/diff": show_diff,
        "/apply": lambda session, _argument: session.apply(),
        "/undo": lambda session, _argument: session.undo(),
        "/goal": goal,
        "/resume": resume,
        "/effort": set_effort,
        "/status": lambda session, _argument: session.status(),
    }

    def close(self) -> None:
        self.record()
        self.events_file.close()
        diff = self.diff()
        if not diff.strip():
            return
        interactive = sys.stdin.isatty() and self.read_line == self._prompt
        if interactive and Confirm.ask("Apply the pending changes to the working tree?", default=False):
            self.apply()
            return
        self.out.print(f"not applied. The diff is saved at {self.run_dir / 'diff.patch'}")
