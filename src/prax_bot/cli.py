from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    no_args_is_help=False,
    help="Prax Bot: a coding agent that works in a Nebius Token Factory Sandbox, powered by NVIDIA Nemotron.",
)
console = Console()

MAX_DIFF_LINES_SHOWN = 400
INIT_TASK = (
    "{what}: a short guide for coding agents working in this repository. Cover, in this order: what the project is "
    "in one sentence; how to install its dependencies; how to run the tests, and the linters or type checkers if it has any; "
    "the layout of the code (which directories hold what); conventions worth knowing (style, naming, where tests go, "
    "commit or changelog rules if documented); and anything surprising a newcomer would trip over. Keep it under 80 lines. "
    "Base every command and statement on files you actually read (pyproject, setup files, CI configs, Makefiles, READMEs, "
    "existing tests); run the test command once to confirm it works, and never invent commands. "
    "Change no file other than AGENTS.md."
)
NO_SANDBOX_ACCESS = (
    "the sandbox API refused the request (403). Sandboxes are in beta and access is granted per project: "
    "request it at https://tokenfactory.nebius.com/sandboxes/about, or run with --backend local meanwhile."
)


def _settings(**kwargs: Any) -> Settings:
    try:
        return Settings.from_env(**kwargs)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None


def _sampling(settings: Settings) -> str:
    parts = [f"temperature {settings.temperature:g}"]
    if settings.top_p is not None:
        parts.append(f"top_p {settings.top_p:g}")
    parts.append(f"effort {settings.effort}")
    return " · ".join(parts)


def _extensions(settings: Settings) -> tuple[list[Any], list[Any]]:
    """Personal skills and plugins from the home directory, announced on the terminal."""
    from .plugins import load_plugins
    from .skills import home_skills
    from .tools import TOOL_MODELS

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


def _apply_diff(root: Path, diff: str, out: Console | None = None) -> bool:
    out = out or console
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as f:
        f.write(diff)
        patch_path = f.name
    try:
        check = subprocess.run(["git", "apply", "--check", patch_path], cwd=root, capture_output=True, text=True)
        if check.returncode != 0:
            out.print(f"[red]the diff does not apply cleanly:[/] {escape(check.stderr.strip())}")
            return False
        applied = subprocess.run(["git", "apply", patch_path], cwd=root, capture_output=True, text=True)
        if applied.returncode != 0:
            out.print(f"[red]git apply failed:[/] {escape(applied.stderr.strip())}")
            return False
    finally:
        os.unlink(patch_path)
    status = subprocess.run(["git", "status", "--short"], cwd=root, capture_output=True, text=True).stdout
    out.print("[green]applied to the working tree[/] (nothing committed)")
    if status.strip():
        out.print(escape(status.rstrip()))
    return True


def _new_run(settings: Settings, verbose: bool, out: Console | None = None) -> tuple[str, Path, Any, Any]:
    """A fresh run directory with its event log and the on_event callback that feeds it and the terminal."""
    from .render import Reporter

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    run_dir = settings.home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    events_file = (run_dir / "events.jsonl").open("a", encoding="utf-8")
    reporter = Reporter(out or console, verbose=verbose)

    def on_event(kind: str, **payload: Any) -> None:
        record = {"t": time.time(), "kind": kind, **{k: _json_safe(v) for k, v in payload.items()}}
        events_file.write(json.dumps(record) + "\n")
        events_file.flush()
        reporter(kind, **payload)

    return run_id, run_dir, events_file, on_event


def _ask_followup() -> str:
    """A further instruction typed by the user, or an empty string to stop."""
    from rich.prompt import Prompt

    try:
        return Prompt.ask("[bold]follow-up[/] [dim](Enter to finish)[/]", default="", show_default=False).strip()
    except (KeyboardInterrupt, EOFError):
        console.print()
        return ""


def _agent_round(
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
    from .agent import Agent, AgentResult
    from .tools import ToolBox

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


def _print_result(
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


def _print_diff(diff: str, ws: Any, run_dir: Path, export: Path | None = None, out: Console | None = None) -> None:
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
    from .llm import LLM
    from .runs import continuation_task

    if ask_followup is None:
        ask_followup = _ask_followup if sys.stdin.isatty() and not yes else (lambda: "")
    llm = LLM(settings)
    original_task = task
    followups: list[str] = []
    steps_total = 0
    round_no = 0
    applied_ok: bool | None = None  # recorded once the user decides; None while undecided or with no diff
    while True:
        round_no += 1
        result = _agent_round(
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

        def write_info() -> None:
            elapsed = time.monotonic() - started
            (run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "task": record_task or original_task,
                        "followups": followups,
                        "resumed_from": resumed_from,
                        "repo": str(root),
                        "model": settings.model,
                        "temperature": settings.temperature,
                        "top_p": settings.top_p,
                        "effort": settings.effort,
                        "investigate_model": settings.investigate_model,
                        "usage_by_model": llm.usage.by_model,
                        "backend": backend,
                        "base_image": settings.base_image if backend == "contree" and not resumed_from else None,
                        "finished": result.finished,
                        "reason": result.reason,
                        "steps": steps_total,
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
                        "applied": applied_ok,
                        **(extra() if callable(extra) else (extra or {})),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

        write_info()
        if diff is None:
            events_file.close()
            console.print(f"[red]✗ could not extract the diff: {escape(diff_error or '')}[/]")
            console.print(f"the agent's final state is image {ws.image_id}; the log is in {run_dir}")
            raise typer.Exit(1)

        _print_result(
            result,
            llm,
            ws,
            run_dir,
            steps_total=steps_total,
            round_steps=result.steps if followups else None,
            elapsed=time.monotonic() - started,
        )
        _print_diff(diff, ws, run_dir, export)

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

    write_info()  # the last round's verdict, e.g. a goal met, is decided after the record above was written
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
        applied_ok = _apply_diff(root, diff)
        write_info()
        if not applied_ok:
            console.print(f"the diff is saved at {run_dir / 'diff.patch'}")
            raise typer.Exit(1)
    else:
        applied_ok = False
        write_info()
        console.print(f"not applied. The diff is saved at {run_dir / 'diff.patch'}")
    if result.reason == "error":
        raise typer.Exit(1)


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
        from .llm import LLM

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
        (self.run_dir / "run.json").write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "task": self.first_task or "(session)",
                    "followups": self.instructions,
                    "session": {"rounds": self.rounds, "applied_rounds": self.applied_rounds},
                    "resumed_from": None,
                    "repo": str(self.root),
                    "model": self.settings.model,
                    "temperature": self.settings.temperature,
                    "top_p": self.settings.top_p,
                    "effort": self.settings.effort,
                    "investigate_model": self.settings.investigate_model,
                    "usage_by_model": self.llm.usage.by_model,
                    "backend": self.backend,
                    "base_image": self.settings.base_image if self.backend == "contree" else None,
                    "finished": bool(r and r.finished),
                    "reason": r.reason if r else "session",
                    "steps": self.steps,
                    "summary": self.last_summary,
                    "model_calls": self.llm.usage.calls,
                    "prompt_tokens": self.llm.usage.prompt_tokens,
                    "completion_tokens": self.llm.usage.completion_tokens,
                    "sandbox_commands": self.ws.commands,
                    "sandbox_cost": self.ws.total_cost,
                    "baseline_image": self.ws.checkpoints[0].image,
                    "final_image": self.ws.image_id,
                    "checkpoints": [dataclasses.asdict(c) for c in self.ws.checkpoints],
                    "elapsed_s": time.monotonic() - self.started,
                    "diff_error": None,
                    "applied": self.applied_rounds > 0,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def run_round(self, task: str, *, goal: bool = False) -> tuple[Any, str]:
        self.round_starts.append(self.ws.current_step)
        result = _agent_round(
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
        _print_result(
            result,
            self.llm,
            self.ws,
            self.run_dir,
            steps_total=self.steps,
            round_steps=result.steps if self.rounds > 1 else None,
            elapsed=time.monotonic() - self.started,
            out=self.out,
        )
        _print_diff(diff, self.ws, self.run_dir, out=self.out)
        return result, diff

    def instruct(self, text: str) -> None:
        from .runs import continuation_task

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

        from .goal import GoalDriver, goal_task
        from .runs import continuation_task

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
        if not _apply_diff(self.root, diff, self.out):
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
        _print_diff(self.diff(), self.ws, self.run_dir, out=self.out)
        self.record()

    def resume(self, argument: str) -> None:
        from .runs import load_run, reopen_workspace

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
        _print_diff(self.diff(), self.ws, self.run_dir, out=self.out)

    def status(self) -> None:
        diff = self.diff()
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_row("model", f"{self.settings.model} · {_sampling(self.settings)}")
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
        if command in ("/quit", "/exit", "/q"):
            return False
        if True:
            if command == "/help":
                self.out.print(SESSION_HELP)
            elif command == "/diff":
                _print_diff(self.diff(), self.ws, self.run_dir, out=self.out)
            elif command == "/apply":
                self.apply()
            elif command == "/undo":
                self.undo()
            elif command == "/goal":
                self.goal(argument)
            elif command == "/resume":
                self.resume(argument)
            elif command == "/effort":
                try:
                    self.settings = _settings(
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
                    return True
                from .llm import LLM

                usage = self.llm.usage
                self.llm = LLM(self.settings)
                self.llm.usage = usage
                self.out.print(f"effort {self.settings.effort}")
            elif command == "/status":
                self.status()
            else:
                self.out.print(f"unknown command {escape(command)}; /help lists them")
        return True

    def close(self) -> None:
        self.record()
        self.events_file.close()
        diff = self.diff()
        if not diff.strip():
            return
        if sys.stdin.isatty() and self.read_line == self._prompt:
            if Confirm.ask("Apply the pending changes to the working tree?", default=False):
                self.apply()
                return
        self.out.print(f"not applied. The diff is saved at {self.run_dir / 'diff.patch'}")


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
) -> None:
    if backend not in {"contree", "local"}:
        raise typer.BadParameter("backend must be 'contree' or 'local'")
    settings = _settings(
        model=model,
        base_image=base_image,
        setup=setup,
        max_steps=max_steps,
        temperature=temperature,
        top_p=top_p,
        effort=effort,
    )
    _require_credentials(settings, need_project=backend == "contree")
    root = _repo_root(repo)
    _header(settings, root, backend, "\n[dim]mode[/]   session: one sandbox, many instructions")
    skills, plugins = _extensions(settings)
    run_id, run_dir, events_file, on_event = _new_run(settings, verbose)
    ws = _open_workspace(settings, backend, root, run_id, on_event)
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
    repo: Path = typer.Option(Path("."), "--repo", "-C", help="A path inside the git repository to work on"),
    model: str | None = typer.Option(None, help="Model id on Token Factory. Default: Nemotron 3 Super"),
    base_image: str | None = typer.Option(None, help="OCI image for the sandbox. Default: python:3.12"),
    setup: str | None = typer.Option(
        None, help="Shell command to run once after upload, e.g. 'pip install -e . pytest'"
    ),
    max_steps: int | None = typer.Option(None, min=1, max=200, help="Model turns per instruction (default 40)"),
    backend: str = typer.Option(
        "contree", help="contree: a Token Factory Sandbox. local: this machine, for development only"
    ),
    protect: list[str] = typer.Option([], "--protect", help="Glob the model may not edit, e.g. 'tests/*'. Repeatable"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show full tool outputs and checkpoints"),
    temperature: float | None = typer.Option(None, min=0.0, max=2.0, help="Sampling temperature (default 1.0)"),
    top_p: float | None = typer.Option(None, "--top-p", min=0.0, max=1.0, help="Nucleus sampling (default 0.95)"),
    effort: str | None = typer.Option(
        None, help="How much the model reasons per step: off, low (default), high, or xhigh"
    ),
) -> None:
    """Open one sandbox on this repository and keep giving it instructions. Also what bare `praxbot` does."""
    _start_session(repo, model, base_image, setup, max_steps, backend, protect, verbose, temperature, top_p, effort)


@app.command()
def ui(
    repo: Path = typer.Option(Path("."), "--repo", "-C", help="A path inside the git repository to work on"),
    model: str | None = typer.Option(None, help="Model id on Token Factory. Default: Nemotron 3 Super"),
    base_image: str | None = typer.Option(None, help="OCI image for the sandbox. Default: python:3.12"),
    setup: str | None = typer.Option(
        None, help="Shell command to run once after upload, e.g. 'pip install -e . pytest'"
    ),
    max_steps: int | None = typer.Option(None, min=1, max=200, help="Model turns per instruction (default 40)"),
    backend: str = typer.Option(
        "contree", help="contree: a Token Factory Sandbox. local: this machine, for development only"
    ),
    protect: list[str] = typer.Option([], "--protect", help="Glob the model may not edit, e.g. 'tests/*'. Repeatable"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show full tool outputs and checkpoints"),
    temperature: float | None = typer.Option(None, min=0.0, max=2.0, help="Sampling temperature (default 1.0)"),
    top_p: float | None = typer.Option(None, "--top-p", min=0.0, max=1.0, help="Nucleus sampling (default 0.95)"),
    effort: str | None = typer.Option(
        None, help="How much the model reasons per step: off, low (default), high, or xhigh"
    ),
) -> None:
    """The full-screen interface: conversation, sidebar, diff pane, one input line. Also what bare `praxbot` does on a terminal."""
    _start_ui(repo, model, base_image, setup, max_steps, backend, protect, verbose, temperature, top_p, effort)


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
) -> None:
    from .tui import run_ui

    if backend not in {"contree", "local"}:
        raise typer.BadParameter("backend must be 'contree' or 'local'")
    settings = _settings(
        model=model,
        base_image=base_image,
        setup=setup,
        max_steps=max_steps,
        temperature=temperature,
        top_p=top_p,
        effort=effort,
    )
    _require_credentials(settings, need_project=backend == "contree")
    root = _repo_root(repo)
    skills, plugins = _extensions(settings)
    run_ui(settings, root, backend, protect, skills, plugins, verbose)


@app.command()
def web(
    repo: Path = typer.Option(Path("."), "--repo", "-C", help="A path inside the git repository to work on"),
    port: int = typer.Option(0, "--port", min=0, max=65535, help="Port on localhost; 0 picks a free one"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the URL instead of opening a browser"),
    model: str | None = typer.Option(None, help="Model id on Token Factory. Default: Nemotron 3 Super"),
    base_image: str | None = typer.Option(None, help="OCI image for the sandbox. Default: python:3.12"),
    setup: str | None = typer.Option(
        None, help="Shell command to run once after upload, e.g. 'pip install -e . pytest'"
    ),
    max_steps: int | None = typer.Option(None, min=1, max=200, help="Model turns per instruction (default 40)"),
    backend: str = typer.Option(
        "contree", help="contree: a Token Factory Sandbox. local: this machine, for development only"
    ),
    protect: list[str] = typer.Option([], "--protect", help="Glob the model may not edit, e.g. 'tests/*'. Repeatable"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show full tool outputs and checkpoints"),
    temperature: float | None = typer.Option(None, min=0.0, max=2.0, help="Sampling temperature (default 1.0)"),
    top_p: float | None = typer.Option(None, "--top-p", min=0.0, max=1.0, help="Nucleus sampling (default 0.95)"),
    effort: str | None = typer.Option(
        None, help="How much the model reasons per step: off, low (default), high, or xhigh"
    ),
) -> None:
    """The web interface on localhost: chat with tool calls, changes, to-dos and rounds in a sidebar, a slash-command menu."""

    if backend not in {"contree", "local"}:
        raise typer.BadParameter("backend must be 'contree' or 'local'")
    settings = _settings(
        model=model,
        base_image=base_image,
        setup=setup,
        max_steps=max_steps,
        temperature=temperature,
        top_p=top_p,
        effort=effort,
    )
    _require_credentials(settings, need_project=backend == "contree")
    root = _repo_root(repo)
    _start_web(settings, root, backend, protect, verbose, port, not no_browser)


def _start_web(
    settings: Settings, root: Path, backend: str, protect: list[str], verbose: bool, port: int, open_browser: bool
) -> None:
    from .web import WebSession, serve

    skills, plugins = _extensions(settings)

    def make_session(out: Console, hook: Any) -> Session:
        run_id, run_dir, events_file, on_event = _new_run(settings, verbose, out=out)

        def on_event_with_hook(kind: str, **payload: Any) -> None:
            on_event(kind, **payload)
            hook(kind, **payload)

        out.print(f"sandbox {settings.base_image}" if backend == "contree" else "local workspace")
        ws = _open_workspace(settings, backend, root, run_id, on_event_with_hook, out=out)
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
        settings = _settings()
        _require_credentials(settings, need_project=True)
        _start_web(settings, _repo_root(Path(".")), "contree", [], False, 0, True)


@app.command()
def status(
    limit: int = typer.Option(10, "--limit", "-n", min=1, max=100, help="How many recent runs to list"),
) -> None:
    """What Prax Bot is doing now, what it did recently, and whether it is set up."""
    from .runs import ago, status_summary

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
    repo: Path = typer.Option(Path("."), "--repo", "-C", help="A path inside the git repository to work on"),
    model: str | None = typer.Option(None, help="Model id on Token Factory. Default: Nemotron 3 Super"),
    investigate_model: str | None = typer.Option(
        None, help="Model for the first investigation turns, e.g. Nemotron 3 Ultra. Default: same as --model"
    ),
    base_image: str | None = typer.Option(None, help="OCI image for the sandbox. Default: python:3.12"),
    setup: str | None = typer.Option(
        None, help="Shell command to run once after upload, e.g. 'pip install -e . pytest'"
    ),
    max_steps: int | None = typer.Option(
        None, min=1, max=200, help="Model turns before the run is stopped (default 40)"
    ),
    backend: str = typer.Option(
        "contree", help="contree: a Token Factory Sandbox. local: this machine, for development only"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply the diff without asking"),
    export: Path | None = typer.Option(None, help="Also write the diff to this file"),
    protect: list[str] = typer.Option([], "--protect", help="Glob the model may not edit, e.g. 'tests/*'. Repeatable"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show full tool outputs and checkpoints"),
    temperature: float | None = typer.Option(
        None, min=0.0, max=2.0, help="Sampling temperature (default 1.0, NVIDIA's recommendation for Nemotron 3)"
    ),
    top_p: float | None = typer.Option(None, "--top-p", min=0.0, max=1.0, help="Nucleus sampling (default 0.95)"),
    effort: str | None = typer.Option(
        None, help="How much the model reasons per step: off, low (default), high, or xhigh"
    ),
) -> None:
    """Solve a task in the current repository. Runs entirely in a sandbox, shows the diff, takes follow-up instructions, then asks before applying."""
    if backend not in {"contree", "local"}:
        raise typer.BadParameter("backend must be 'contree' or 'local'")
    settings = _settings(
        model=model,
        investigate_model=investigate_model,
        base_image=base_image,
        setup=setup,
        max_steps=max_steps,
        temperature=temperature,
        top_p=top_p,
        effort=effort,
    )
    _require_credentials(settings, need_project=backend == "contree")
    root = _repo_root(repo)

    _header(settings, root, backend)
    console.print(f"[bold]task[/]  {escape(task)}")
    skills, plugins = _extensions(settings)
    console.print()

    run_id, run_dir, events_file, on_event = _new_run(settings, verbose)
    started = time.monotonic()
    ws = _open_workspace(settings, backend, root, run_id, on_event)
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
    repo: Path = typer.Option(Path("."), "--repo", "-C", help="A path inside the git repository"),
    force: bool = typer.Option(False, "--force", help="Write AGENTS.md even if one exists"),
    model: str | None = typer.Option(None, help="Model id on Token Factory. Default: Nemotron 3 Super"),
    base_image: str | None = typer.Option(None, help="OCI image for the sandbox. Default: python:3.12"),
    setup: str | None = typer.Option(
        None, help="Shell command to run once after upload, e.g. 'pip install -e . pytest'"
    ),
    max_steps: int | None = typer.Option(
        None, min=1, max=200, help="Model turns before the run is stopped (default 40)"
    ),
    backend: str = typer.Option(
        "contree", help="contree: a Token Factory Sandbox. local: this machine, for development only"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Write the file without asking"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show full tool outputs and checkpoints"),
) -> None:
    """Write an AGENTS.md for this repository: how to install, test and lint it, its layout and conventions, from what the agent finds."""
    if backend not in {"contree", "local"}:
        raise typer.BadParameter("backend must be 'contree' or 'local'")
    settings = _settings(model=model, base_image=base_image, setup=setup, max_steps=max_steps)
    _require_credentials(settings, need_project=backend == "contree")
    root = _repo_root(repo)
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
    skills, plugins = _extensions(settings)
    console.print()
    run_id, run_dir, events_file, on_event = _new_run(settings, verbose)
    started = time.monotonic()
    ws = _open_workspace(settings, backend, root, run_id, on_event)
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


def _open_workspace(
    settings: Settings, backend: str, root: Path, run_id: str, on_event: Any, out: Console | None = None
) -> Any:
    from contree_sdk.sdk.exceptions import ForbiddenError

    from .sandbox import WorkspaceError, create_workspace, make_client

    interface = out is not None
    out = out or console
    if not interface:
        out.print("[bold]sandbox[/]" if backend == "contree" else "[bold]local workspace[/]")
    try:
        if backend == "local":
            from .local import create_local_workspace

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
            + f" · {_sampling(settings)}\n"
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
    repo: Path = typer.Option(Path("."), "--repo", "-C", help="A path inside the git repository to work on"),
    model: str | None = typer.Option(None, help="Model id on Token Factory. Default: Nemotron 3 Super"),
    base_image: str | None = typer.Option(None, help="OCI image for the sandbox. Default: python:3.12"),
    setup: str | None = typer.Option(
        None, help="Shell command to run once after upload, e.g. 'pip install -e . pytest'"
    ),
    max_steps: int | None = typer.Option(None, min=1, max=200, help="Model turns per round (default 40)"),
    backend: str = typer.Option(
        "contree", help="contree: a Token Factory Sandbox. local: this machine, for development only"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply the diff without asking"),
    export: Path | None = typer.Option(None, help="Also write the diff to this file"),
    protect: list[str] = typer.Option([], "--protect", help="Glob the model may not edit, e.g. 'tests/*'. Repeatable"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show full tool outputs and checkpoints"),
    temperature: float | None = typer.Option(
        None, min=0.0, max=2.0, help="Sampling temperature (default 1.0, NVIDIA's recommendation for Nemotron 3)"
    ),
    top_p: float | None = typer.Option(None, "--top-p", min=0.0, max=1.0, help="Nucleus sampling (default 0.95)"),
    effort: str | None = typer.Option(
        None, help="How much the model reasons per step: off, low (default), high, or xhigh"
    ),
) -> None:
    """Work toward a goal in rounds until a check command passes or the model reports it met, then show the diff."""
    from .goal import GoalDriver, goal_task

    if backend not in {"contree", "local"}:
        raise typer.BadParameter("backend must be 'contree' or 'local'")
    settings = _settings(
        model=model,
        base_image=base_image,
        setup=setup,
        max_steps=max_steps,
        temperature=temperature,
        top_p=top_p,
        effort=effort,
    )
    _require_credentials(settings, need_project=backend == "contree")
    root = _repo_root(repo)
    _header(
        settings,
        root,
        backend,
        f"\n[dim]goal[/]   up to {rounds} rounds"
        + (f", met when `{escape(check)}` exits 0" if check else ", the model decides when it is met"),
    )
    console.print(f"[bold]goal[/]  {escape(objective)}")
    skills, plugins = _extensions(settings)
    console.print()

    run_id, run_dir, events_file, on_event = _new_run(settings, verbose)
    started = time.monotonic()
    ws = _open_workspace(settings, backend, root, run_id, on_event)
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
    max_steps: int | None = typer.Option(
        None, min=1, max=200, help="Model turns before the run is stopped (default 40)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply the diff without asking"),
    export: Path | None = typer.Option(None, help="Also write the diff to this file"),
    protect: list[str] = typer.Option([], "--protect", help="Glob the model may not edit. Repeatable"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show full tool outputs and checkpoints"),
    temperature: float | None = typer.Option(
        None, min=0.0, max=2.0, help="Sampling temperature (default 1.0, NVIDIA's recommendation for Nemotron 3)"
    ),
    top_p: float | None = typer.Option(None, "--top-p", min=0.0, max=1.0, help="Nucleus sampling (default 0.95)"),
    effort: str | None = typer.Option(
        None, help="How much the model reasons per step: off, low (default), high, or xhigh"
    ),
) -> None:
    """Continue a past run from one of its checkpoints, in a workspace rebuilt from that image."""
    from contree_sdk.sdk.exceptions import ForbiddenError

    from .runs import continuation_task, latest_run_id, load_run, reopen_workspace
    from .sandbox import WorkspaceError

    settings = _settings(model=model, max_steps=max_steps, temperature=temperature, top_p=top_p, effort=effort)
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
    if model is None and info.get("model"):
        settings = dataclasses.replace(settings, model=str(info["model"]))
    _require_credentials(settings, need_project=backend == "contree")
    root = _repo_root(Path(str(info.get("repo") or ".")))
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
    skills, plugins = _extensions(settings)
    console.print()

    new_run_id, run_dir, events_file, on_event = _new_run(settings, verbose)
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
    from .runs import checkpoint_diff, latest_run_id, list_runs, load_run

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
        if not _apply_diff(root, text):
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
    from .render import describe_call

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
