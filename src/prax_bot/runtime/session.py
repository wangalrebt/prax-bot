"""Session execution and state shared by every interface.

The runtime emits events and returns results. Terminal prompts, slash-command parsing,
and rendering belong to the interfaces.
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any, TextIO

from prax_bot.core.agent import Agent, AgentResult
from prax_bot.core.goal import GoalDriver, goal_task
from prax_bot.core.prompts import prompt
from prax_bot.core.tools import ToolBox
from prax_bot.core.verification import CheckResult, VerificationStatus, run_check, verification_status
from prax_bot.extensions.plugins import ExtraTool
from prax_bot.extensions.skills import Skill
from prax_bot.runtime.apply import ApplyTransaction
from prax_bot.runtime.runs import build_record, continuation_task, load_run, reopen_workspace, write_record
from prax_bot.settings import EFFORT_LEVELS, Settings
from prax_bot.workspace.types import Changes, EventFn, Workspace, WorkspaceError

if TYPE_CHECKING:
    from prax_bot.core.llm import LLM


@dataclass
class RoundState:
    checkpoint: int
    first_task: str | None
    instructions: list[str]
    summary: str
    result: AgentResult | None
    rounds: int
    goal_info: dict[str, Any] | None


class Session:
    """One workspace, a series of instructions, and the evidence produced by each round."""

    def __init__(
        self,
        settings: Settings,
        ws: Workspace,
        root: Path,
        *,
        backend: str,
        run_id: str,
        run_dir: Path,
        events_file: TextIO,
        on_event: EventFn,
        protect: Sequence[str] = (),
        skills: Sequence[Skill] = (),
        plugins: Sequence[ExtraTool] = (),
        llm: LLM | None = None,
        kind: str = "session",
    ) -> None:
        self.settings = dataclasses.replace(settings, backend=backend)
        self.ws, self.root, self.backend = ws, root.resolve(), backend
        self.run_id, self.run_dir, self.events_file = run_id, run_dir, events_file
        self.kind = kind
        self._observer = on_event
        self.protect, self.skills, self.plugins = tuple(protect), tuple(skills), tuple(plugins)
        if llm is None:
            from prax_bot.core.llm import LLM

            llm = LLM(self.settings)
        self.llm = llm
        self.started = time.monotonic()
        self.first_task: str | None = None
        self.task_label: str | None = None  # how the record names the task when that differs from its text
        self.instructions: list[str] = []
        self.last_result: AgentResult | None = None
        self.last_summary = ""
        self.steps = 0
        self.rounds = 0
        self.applied_rounds = 0
        self.has_applied = False
        self.last_diff = ""
        self.changes: Changes | None = None
        self.diff_error: str | None = None
        self.checks: list[CheckResult] = []
        self._expected_checks: tuple[tuple[str, str], ...] = ()
        self._goal_check: str | None = None
        self.resumed_from: dict[str, Any] | None = None
        self.goal_info: dict[str, Any] | None = None
        self.active = False
        self.closed = False
        self.cancel = Event()
        self._round_states: list[RoundState] = []
        self._pending_apply: ApplyTransaction | None = None
        self._agent: Agent | None = None
        self.ws.on_event = self.on_event
        self.record()

    def on_event(self, kind: str, **payload: Any) -> None:
        self._observer(kind, **payload)
        if kind == "checkpoint":
            self.record()

    def request_stop(self) -> None:
        """Signal the worker; the current model or tool call is allowed to finish."""
        self.cancel.set()
        self.on_event("session.stop_requested")

    def diff(self) -> str:
        try:
            self.changes = self.ws.changes()
            self.last_diff = self.changes.patch
            (self.run_dir / "diff.patch").write_text(self.last_diff, encoding="utf-8")
            self.diff_error = None
        except Exception as error:
            self.changes, self.last_diff = None, ""
            self.diff_error = f"{type(error).__name__}: {str(error)[:500]}"
            patch = self.run_dir / "diff.patch"
            if patch.exists():
                patch.replace(self.run_dir / "last-valid-diff.patch")
            self.record()
            raise WorkspaceError(f"could not extract the diff: {self.diff_error}") from error
        return self.last_diff

    def record(self) -> None:
        pending = self._pending_apply.metadata if self._pending_apply is not None else None
        steps = self.steps + (self._agent.steps if self._agent is not None else 0)
        extra = {
            "session": {"rounds": self.rounds, "applied_rounds": self.applied_rounds}
            if self.kind == "session"
            else None,
            "kind": self.kind,
            "state": "closed" if self.closed else "working" if self.active else "idle",
            "activity_tracking": "event_lock",
            "pending_apply": pending,
            "verification": {
                "status": self.verification_status,
                "checks": [dataclasses.asdict(check) for check in self.checks],
            },
            "test_command": self.settings.test_command,
            "lint_command": self.settings.lint_command,
            "context_budget": self.settings.context_budget,
        }
        if self.backend == "local":
            from prax_bot.workspace.local import LocalWorkspace

            if isinstance(self.ws, LocalWorkspace):
                extra["workspace_root"] = str(self.ws.root)
        if self.goal_info is not None:
            extra["goal"] = self.goal_info
        write_record(
            self.run_dir,
            build_record(
                run_id=self.run_id,
                task=self.task_label or self.first_task or "(session)",
                followups=self.instructions,
                repo=self.root,
                settings=self.settings,
                llm=self.llm,
                ws=self.ws,
                backend=self.backend,
                base_image=self.settings.base_image if self.backend == "contree" else None,
                result=self.last_result,
                steps=steps,
                summary=self.last_summary,
                elapsed_s=time.monotonic() - self.started,
                resumed_from=self.resumed_from,
                diff_error=self.diff_error,
                applied=self.has_applied,
                extra=extra,
            ),
        )

    def _require_ready(self) -> None:
        if self.closed:
            raise RuntimeError("the session is closed")
        if self.active:
            raise RuntimeError("the session is already working")
        if self._pending_apply is not None:
            raise WorkspaceError("an apply is unfinished; retry /apply to complete it first")

    def run_round(
        self,
        task: str,
        *,
        goal: bool = False,
        instruction: str | None = None,
        original_task: str | None = None,
    ) -> tuple[AgentResult, str]:
        self._require_ready()
        self._round_states.append(
            RoundState(
                self.ws.current_step,
                self.first_task,
                list(self.instructions),
                self.last_summary,
                self.last_result,
                self.rounds,
                dict(self.goal_info) if self.goal_info is not None else None,
            )
        )
        if self.first_task is None:
            self.first_task = instruction or task
            self.task_label = original_task
        elif instruction is not None:
            self.instructions.append(instruction)
            self.on_event("followup", instruction=instruction)
        self.on_event("instruction", text=instruction or original_task or task)
        self.checks = []
        self._expected_checks = tuple((name, command) for name, command in self.check_commands if command)
        configured = [(name, command) for name, command in self.check_commands if command]
        if configured:
            task += "\n\n" + prompt(
                "verification", commands="\n".join(f"- {name}: {command}" for name, command in configured)
            )
        agent = Agent(
            self.llm,
            ToolBox(self.ws, protected=self.protect, skills=self.skills, plugins=self.plugins),
            max_steps=self.settings.max_steps,
            on_event=self.on_event,
            investigate_model=self.settings.investigate_model,
            investigate_steps=self.settings.investigate_steps,
            goal=goal,
            context_budget=self.settings.context_budget,
            system_prompt=self.settings.system_prompt_text(),
            cancel=self.cancel,
        )
        self._agent = agent
        self.active = True
        try:
            self.record()
            try:
                result = agent.run(task)
            except KeyboardInterrupt:
                self.cancel.set()
                result = agent.cancelled()
            except Exception as error:
                result = AgentResult(
                    False, f"stopped by an error: {type(error).__name__}: {str(error)[:400]}", agent.steps, "error"
                )
            self._agent = None
            self.steps += result.steps
            self.rounds += 1
            self.last_result, self.last_summary = result, result.summary
            if result.reason not in ("cancelled", "error"):
                try:
                    self._verify()
                except KeyboardInterrupt:
                    self.cancel.set()
                if self.cancel.is_set():
                    result.finished, result.reason, result.goal_met = False, "cancelled", False
                    result.summary += "\nStopped by the user during verification."
            self.last_summary = result.summary
            diff = self.diff()
            self.on_event(
                "session.round",
                result=result,
                steps_total=self.steps,
                round_steps=result.steps,
                elapsed=time.monotonic() - self.started,
                usage=self.llm.usage,
                commands=self.ws.commands,
                checkpoints=len(self.ws.checkpoints),
                cost=self.ws.total_cost,
                run_dir=str(self.run_dir),
                diff=diff,
                verification=self.verification_status,
            )
            return result, diff
        finally:
            self._agent = None
            self.active = False
            self.cancel.clear()
            self.record()

    def instruct(self, text: str, *, goal: bool = False) -> tuple[AgentResult, str]:
        task = (
            text
            if self.first_task is None
            else continuation_task(self.first_task, self.last_summary, self.diff(), text)
        )
        return self.run_round(task, goal=goal, instruction=text)

    @property
    def check_commands(self) -> tuple[tuple[str, str | None], ...]:
        commands = (("tests", self.settings.test_command), ("lint", self.settings.lint_command))
        if self._goal_check and all(command != self._goal_check for _, command in commands):
            commands += (("goal", self._goal_check),)
        return commands

    @property
    def verification_status(self) -> VerificationStatus:
        configured = self._expected_checks or tuple((name, command) for name, command in self.check_commands if command)
        return verification_status(configured, self.checks, self.ws.image_id)

    def _verify(self) -> None:
        self.checks = []
        self._expected_checks = tuple((name, command) for name, command in self.check_commands if command)
        for name, command in self._expected_checks:
            if not command or self.cancel.is_set():
                continue
            self.on_event("verification.start", name=name, command=command)
            result = run_check(self.ws, name, command, self.settings.command_timeout)
            self.checks.append(result)
            self.on_event("verification.result", **dataclasses.asdict(result), passed=result.passed)
            self.record()

    def verify(self) -> list[CheckResult]:
        self._require_ready()
        self.active = True
        try:
            self._verify()
            return list(self.checks)
        finally:
            self.active = False
            self.cancel.clear()
            self.record()

    def run_goal(self, objective: str, *, max_rounds: int = 5, check: str | None = None) -> GoalDriver:
        self._goal_check = check
        try:
            driver = GoalDriver(objective, max_rounds=max_rounds, check=check)
            self.goal_info = {
                "objective": objective,
                "check": check,
                "max_rounds": max_rounds,
                "rounds": 0,
                "met": False,
            }
            first = goal_task(objective, max_rounds, check)
            task = (
                first
                if self.first_task is None
                else continuation_task(self.first_task, self.last_summary, self.diff(), first)
            )
            instruction = objective
            for number in range(1, max_rounds + 1):
                result, diff = self.run_round(task, goal=True, original_task=objective, instruction=instruction)
                next_task = driver.next_instruction(
                    number,
                    result,
                    diff,
                    verification=self.verification_status,
                    check_output="\n".join(
                        item.output or item.error or "" for item in self.checks if item.command == check
                    ),
                )
                self.goal_info.update(rounds=number, met=driver.met, stopped=driver.stopped)
                self.on_event("goal.round", round=number, met=driver.met, stopped=driver.stopped)
                self.record()
                if next_task is None:
                    return driver
                instruction = next_task
                failures = "\n".join(f"{c.command}: {c.output or c.error}" for c in self.checks if not c.passed)
                task = continuation_task(
                    objective,
                    result.summary,
                    diff,
                    next_task + ("\nConfigured checks failed:\n" + failures if failures else ""),
                )
            return driver
        finally:
            self._goal_check = None

    def apply(self, paths: Sequence[str] | None = None) -> bool:
        if self.active or self.closed:
            raise RuntimeError("wait for the current work to stop before applying changes")
        if self._pending_apply is not None:
            self._finish_apply()
            self.diff()
            self.record()
            return True
        self.diff()  # A failed refresh must never authorize applying the previous patch.
        assert self.changes is not None
        if paths is None and self.changes.flagged:
            paths = [name for name in self.changes.files if name not in self.changes.flagged]
        changes = self.ws.changes(paths) if paths is not None else self.changes
        if not changes.patch.strip():
            return False
        self._pending_apply = ApplyTransaction.prepare(self.run_dir, self.root, changes)
        self.record()
        self._finish_apply()
        self.diff()
        self.record()
        return True

    def _finish_apply(self) -> None:
        assert self._pending_apply is not None
        transaction = self._pending_apply
        transaction.finish(self.root, self.ws)
        self._pending_apply = None
        self.has_applied = True
        self.applied_rounds = self.rounds
        self._round_states.clear()
        try:
            self.record()
            transaction.clear()
        except BaseException:
            self._pending_apply = transaction
            raise
        self.on_event("session.applied", files=list(transaction.changes.files))

    def undo(self) -> bool:
        self._require_ready()
        if not self._round_states:
            return False
        previous = self._round_states[-1]
        self.ws.restore(previous.checkpoint)
        self._round_states.pop()
        self.first_task, self.instructions = previous.first_task, previous.instructions
        self.last_summary, self.last_result, self.rounds = previous.summary, previous.result, previous.rounds
        self.goal_info = previous.goal_info
        self.checks = []
        self._expected_checks = ()
        self.diff()
        self.record()
        self.on_event("session.undone", checkpoint=previous.checkpoint)
        return True

    def resume(self, run_id: str, step: int | None = None) -> None:
        self._require_ready()
        past = load_run(run_id, self.settings)
        if not past.info.get("repo") or Path(past.info["repo"]).resolve() != self.root:
            raise ValueError("that run belongs to a different repository; open it with prax resume RUN-ID")
        if past.info.get("backend", "contree") != self.backend:
            raise ValueError("that run uses a different backend; open it with prax resume RUN-ID")
        step = len(past.checkpoints) - 1 if step is None else step
        workspace = reopen_workspace(
            past, step, self.settings, self._observer, f"{self.run_id}-resume-{uuid.uuid4().hex[:8]}"
        )
        changes = workspace.changes()
        self.ws = workspace
        self.ws.on_event = self.on_event
        self.first_task = str(past.info.get("task") or "") or None
        self.instructions = list(past.info.get("followups") or [])
        self.last_summary = str(past.info.get("summary") or "")
        self.last_result = None
        self.goal_info = None
        self.checks = []
        self._expected_checks = ()
        self._round_states.clear()
        self.resumed_from = {"run_id": run_id, "step": step, "image": past.checkpoints[step]["image"]}
        self.changes, self.last_diff, self.diff_error = changes, changes.patch, None
        (self.run_dir / "diff.patch").write_text(self.last_diff, encoding="utf-8")
        self.record()
        self.on_event("session.resumed", run_id=run_id, checkpoint=step)

    def set_effort(self, level: str) -> None:
        level = level.strip().lower()
        if level not in EFFORT_LEVELS:
            raise ValueError(f"effort must be one of {', '.join(EFFORT_LEVELS)}")
        self._require_ready()
        self.settings = dataclasses.replace(
            self.settings, effort=level, sources={**self.settings.sources, "effort": "session"}
        )
        self.llm.effort = level
        self.record()
        self.on_event("session.effort", effort=level)

    def state(self) -> dict[str, Any]:
        return {
            "repo": str(self.root),
            "name": self.root.name,
            "backend": self.backend,
            "model": self.settings.model,
            "effort": self.settings.effort,
            "rounds": self.rounds,
            "steps": self.steps,
            "tokens": self.llm.usage.total_tokens,
            "context": int(getattr(self.llm, "last_prompt_tokens", 0) or 0),
            "context_budget": self.settings.context_budget,
            "calls": self.llm.usage.calls,
            "checkpoints": len(self.ws.checkpoints),
            "cost": self.ws.total_cost,
            "files": list(self.changes.files) if self.changes is not None else [],
            "excluded": list(self.changes.excluded) if self.changes is not None else [],
            "scratch": list(self.changes.scratch) if self.changes is not None else [],
            "binary": list(self.changes.binary) if self.changes is not None else [],
            "diff": self.last_diff,
            "diff_error": self.diff_error,
            "summary": self.last_summary,
            "applied_rounds": self.applied_rounds,
            "pending_apply": self._pending_apply is not None,
            "can_undo": bool(self._round_states),
            "verification": self.verification_status,
            "checks": [dataclasses.asdict(check) for check in self.checks],
            "log": str(self.run_dir),
            "closed": self.closed,
            "stopping": self.cancel.is_set(),
        }

    def close(self) -> None:
        if self.closed:
            return
        if self.active:
            self.request_stop()
            raise RuntimeError("the session will close after its current call finishes")
        try:
            self.diff()
        finally:
            self.closed = True
            try:
                self.record()
            finally:
                self.events_file.close()
