"""Slash commands and the plain terminal session."""

from __future__ import annotations

import shlex
import sys
from collections.abc import Callable
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm, Prompt
from rich.table import Table

from prax_bot.runtime.session import Session
from prax_bot.ui.render import print_diff
from prax_bot.workspace.types import WorkspaceError

QUIT_COMMANDS = ("/quit", "/exit", "/q")
SESSION_HELP = """Type an instruction to work on the current workspace.
  /diff                   show pending changes and ignored paths
  /apply [PATH ...]        apply all pending files, or only the named files
  /undo                   restore the workspace and context before the last round
  /goal [--check CMD] [--rounds N] OBJECTIVE   work in rounds toward an objective
  /compare [--a APPROACH] [--b APPROACH] OBJECTIVE   compare two fixes from the same checkpoint
  /choose A|B             choose a compared fix; /apply then writes it to the repository
  /resume RUN [--from N]   resume a checkpoint from this repository and backend
  /effort LEVEL           off, low, high or xhigh
  /verify                 run the configured test and lint commands
  /stop                   stop after the current model or tool call
  /status                 execution, verification and pending changes
  /help                   this text
  /quit                   leave the session"""


def goal_arguments(argument: str) -> tuple[str, int, str | None]:
    words = shlex.split(argument)
    objective: list[str] = []
    rounds, check = 5, None
    i = 0
    while i < len(words):
        word = words[i]
        if word == "--":
            objective.extend(words[i + 1 :])
            break
        if word in ("--check", "--rounds"):
            if i + 1 == len(words):
                raise ValueError(f"{word} needs a value")
            if word == "--check":
                check = words[i + 1]
            else:
                rounds = int(words[i + 1])
                if not 1 <= rounds <= 50:
                    raise ValueError("rounds must be between 1 and 50")
            i += 2
        else:
            objective.append(word)
            i += 1
    text = " ".join(objective).strip()
    if not text:
        raise ValueError("usage: /goal [--check CMD] [--rounds N] OBJECTIVE")
    return text, rounds, check


def comparison_arguments(argument: str) -> tuple[str, str, str]:
    words = shlex.split(argument)
    objective: list[str] = []
    approaches = {"--a": "", "--b": ""}
    i = 0
    while i < len(words):
        word = words[i]
        if word == "--":
            objective.extend(words[i + 1 :])
            break
        if word in approaches:
            if i + 1 == len(words):
                raise ValueError(f"{word} needs a value")
            approaches[word] = words[i + 1]
            i += 2
        elif word.startswith("--"):
            raise ValueError(f"unknown comparison option {word}; use -- before an objective starting with --")
        else:
            objective.append(word)
            i += 1
    text = " ".join(objective).strip()
    if not text:
        raise ValueError("usage: /compare [--a APPROACH] [--b APPROACH] OBJECTIVE")
    return text, approaches["--a"], approaches["--b"]


def choice_arguments(argument: str) -> tuple[str, str | None]:
    words = shlex.split(argument)
    if not words or words[0] not in ("A", "B"):
        raise ValueError("usage: /choose A|B [--comparison ID]")
    if len(words) == 1:
        return words[0], None
    if len(words) == 3 and words[1] == "--comparison" and words[2].strip():
        return words[0], words[2]
    raise ValueError("usage: /choose A|B [--comparison ID]")


def show_changes(session: Session, out: Console) -> None:
    print_diff(session.diff(), session.run_dir, out=out)
    if session.changes and session.changes.excluded:
        out.print("[dim]excluded by Git ignore rules (project or workspace):[/]")
        for name in session.changes.excluded:
            out.print(f"  {escape(name)}")
    if session.changes and session.changes.flagged:
        out.print("[dim]in the diff but left out of /apply unless named:[/]")
        for name in session.changes.scratch:
            out.print(f"  {escape(name)}  [dim]looks like a scratch script[/]")
        for name in session.changes.binary:
            out.print(f"  {escape(name)}  [dim]binary file the run created[/]")


def show_comparison(comparison: dict[str, Any], out: Console) -> None:
    out.print(f"comparison {escape(comparison['id'])}: {escape(comparison['status'])}")
    table = Table("Candidate", "Run", "Result", "Summary", box=None)
    for candidate in comparison["candidates"]:
        table.add_row(
            candidate["label"],
            escape(candidate.get("run_id") or "not started"),
            escape(f"{candidate['status']} · checks {candidate['verification']}"),
            escape(candidate.get("error") or candidate.get("summary") or ""),
        )
    out.print(table)
    if comparison.get("stale"):
        out.print("comparison is stale; run /compare again before choosing")
    elif comparison.get("selected"):
        out.print(
            f"candidate {comparison['selected']} is selected; review /diff, then /apply to write it to the repository"
        )
    else:
        out.print("choose explicitly with /choose A or /choose B, then /apply to write it to the repository")


def show_status(session: Session, out: Console) -> None:
    session.diff()
    state = session.state()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("model", f"{state['model']} · effort {state['effort']}")
    table.add_row("rounds", f"{state['rounds']} · {state['steps']} steps · {state['tokens']:,} tokens")
    table.add_row("context", f"{state['context']:,} tokens; budget {state['context_budget']:,}")
    table.add_row("workspace", f"{state['backend']} · {state['checkpoints']} checkpoints · cost {state['cost']:.4f}")
    table.add_row("checks", state["verification"])
    table.add_row("changes", f"{len(state['files'])} files pending")
    table.add_row("log", state["log"])
    out.print(table)
    if state.get("comparison") is not None:
        show_comparison(state["comparison"], out)


def handle_line(session: Session, line: str, out: Console) -> bool:
    line = line.strip()
    if not line:
        return True
    if not line.startswith("/"):
        session.instruct(line)
        return True
    command, _, argument = line.partition(" ")
    command = command.lower()
    if command in QUIT_COMMANDS:
        return False
    try:
        if command == "/help":
            out.print(SESSION_HELP, markup=False)
        elif command == "/diff":
            show_changes(session, out)
        elif command == "/apply":
            paths = shlex.split(argument) if argument.strip() else None
            if paths and paths[0] == "--":
                paths = paths[1:]
            if not session.apply(paths):
                out.print("nothing to apply")
        elif command == "/undo":
            if not session.undo():
                out.print("nothing to undo")
        elif command == "/effort":
            session.set_effort(argument)
        elif command == "/verify":
            if not session.verify():
                out.print("no checks configured; set test_command and/or lint_command in your config")
        elif command == "/stop":
            if session.active:
                session.request_stop()
            else:
                out.print("nothing is running")
        elif command == "/status":
            show_status(session, out)
        elif command == "/goal":
            objective, rounds, check = goal_arguments(argument)
            driver = session.run_goal(objective, max_rounds=rounds, check=check)
            out.print(escape(driver.status))
        elif command == "/compare":
            objective, approach_a, approach_b = comparison_arguments(argument)
            comparison = session.compare(objective, approach_a=approach_a, approach_b=approach_b)
            show_comparison(comparison, out)
        elif command == "/choose":
            label, comparison_id = choice_arguments(argument)
            session.choose_candidate(label, comparison_id=comparison_id)
            out.print(f"chose candidate {label}; review /diff, then /apply to write it to the repository")
        elif command == "/resume":
            words = shlex.split(argument)
            if len(words) == 1:
                session.resume(words[0])
            elif len(words) == 3 and words[1] == "--from":
                session.resume(words[0], int(words[2]))
            else:
                raise ValueError("usage: /resume RUN-ID [--from N]")
            show_changes(session, out)
        else:
            out.print(f"unknown command {escape(command)}; /help lists them")
    except (ValueError, WorkspaceError, RuntimeError) as error:
        out.print(f"[red]✗ {escape(str(error))}[/]")
    return True


def session_loop(session: Session, out: Console, read_line: Callable[[], str | None] | None = None) -> None:
    def read() -> str | None:
        try:
            return Prompt.ask("\n[bold cyan]praxbot[/]", default="", show_default=False, console=out)
        except (KeyboardInterrupt, EOFError):
            return None

    reader = read_line or read
    out.print("[dim]type an instruction, or /help[/]")
    try:
        while True:
            line = reader()
            if line is None:
                break
            try:
                if not handle_line(session, line, out):
                    break
            except Exception as error:
                out.print(f"[red]✗ {escape(str(error))}[/]")
        if session.diff().strip():
            if (
                read_line is None
                and sys.stdin.isatty()
                and Confirm.ask("Apply the pending changes?", default=False, console=out)
            ):
                session.apply()
            else:
                out.print(f"pending diff saved at {session.run_dir / 'diff.patch'}")
    finally:
        try:
            session.close()
        except WorkspaceError as error:
            out.print(f"[red]✗ {escape(str(error))}[/]")
