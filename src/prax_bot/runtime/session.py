"""Session execution and state shared by every interface.

The runtime emits events and returns results. Terminal prompts, slash-command parsing,
and rendering belong to the interfaces.
"""

from __future__ import annotations

import dataclasses
import time
import traceback
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import TYPE_CHECKING, Any, TextIO

from prax_bot.core.agent import Agent, AgentResult
from prax_bot.core.goal import GoalDriver, goal_task
from prax_bot.core.tools import ToolBox
from prax_bot.core.verification import CheckResult, VerificationStatus, attributed, run_check, verification_status
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


@dataclass
class BaselineRun:
    """A configured check running on the clean image in a parallel fork, and its result once it is back."""

    thread: Thread | None = None
    result: CheckResult | None = None
    reported: bool = False


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
        self._baselines: dict[tuple[str, str], BaselineRun] = {}  # by (clean image, command)
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
                "checks": [check.as_dict() for check in self.checks],
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
        self._round_states.append(self._snapshot())
        if self.first_task is None:
            self.first_task = instruction or task
            self.task_label = original_task
        elif instruction is not None:
            self.instructions.append(instruction)
            self.on_event("followup", instruction=instruction)
        self.on_event("instruction", text=instruction or original_task or task)
        self.checks = []
        self._expected_checks = self.check_commands
        self._start_baselines()
        agent = Agent(
            self.llm,
            ToolBox(
                self.ws,
                protected=self.protect,
                skills=self.skills,
                plugins=self.plugins,
                known_failures=self._known_failures,
            ),
            max_steps=self.settings.max_steps,
            on_event=self.on_event,
            investigate_model=self.settings.investigate_model,
            investigate_steps=self.settings.investigate_steps,
            goal=goal,
            context_budget=self.settings.context_budget,
            system_prompt=self.settings.system_prompt_text(),
            verification_commands=self._expected_checks,
            verification_in_fork=self.ws.clean is not None,
            cancel=self.cancel,
        )
        self._agent = agent
        self.active = True
        try:
            self.record()
            result = self._run_agent(agent, task)
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
            diff = self._current_diff()
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

    def _snapshot(self) -> RoundState:
        """What /undo restores besides the workspace: the conversation context before this round."""
        return RoundState(
            self.ws.current_step,
            self.first_task,
            list(self.instructions),
            self.last_summary,
            self.last_result,
            self.rounds,
            dict(self.goal_info) if self.goal_info is not None else None,
        )

    def _run_agent(self, agent: Agent, task: str) -> AgentResult:
        """One agent transcript. An interrupt cancels it; any other failure is recorded with its traceback."""
        try:
            return agent.run(task)
        except KeyboardInterrupt:
            self.cancel.set()
            return agent.cancelled()
        except Exception as error:
            message = f"{type(error).__name__}: {str(error)[:400]}"
            self.on_event("error", step=agent.steps, error=message, traceback=traceback.format_exc())
            return AgentResult(False, f"stopped by an error: {message}", agent.steps, "error")

    def instruct(self, text: str, *, goal: bool = False) -> tuple[AgentResult, str]:
        task = (
            text
            if self.first_task is None
            else continuation_task(self.first_task, self.last_summary, self.diff(), text)
        )
        return self.run_round(task, goal=goal, instruction=text)

    @property
    def check_commands(self) -> tuple[tuple[str, str], ...]:
        """The checks run after a round: the configured test and lint commands, plus a goal's own check."""
        configured = (("tests", self.settings.test_command), ("lint", self.settings.lint_command))
        commands = [(name, command) for name, command in configured if command]
        if self._goal_check and all(command != self._goal_check for _, command in commands):
            commands.append(("goal", self._goal_check))
        return tuple(commands)

    @property
    def verification_status(self) -> VerificationStatus:
        return verification_status(self._expected_checks or self.check_commands, self.checks, self.ws.image_id)

    def _current_diff(self) -> str:
        """The diff of the workspace as it stands, read again only if the workspace moved since the last read."""
        if self.changes is not None and self.changes.image == self.ws.image_id and self.diff_error is None:
            return self.last_diff
        return self.diff()

    def _applicable_changes(self) -> Changes:
        """What an apply that names no files would write: the diff without the flagged scratch and binary files."""
        self._current_diff()
        assert self.changes is not None
        if not self.changes.flagged:
            return self.changes
        return self.ws.changes([name for name in self.changes.files if name not in self.changes.flagged])

    def _start_baselines(self) -> None:
        """Start every configured check on the clean image in a parallel fork, unless that run already exists.

        It costs the round no time, and its result says which failures were there before the change.
        """
        clean = self.ws.clean
        if clean is None:
            return
        for name, command in self.check_commands:
            if (clean, command) in self._baselines:
                continue
            run = self._baselines[(clean, command)] = BaselineRun()
            run.thread = Thread(
                target=self._run_baseline, args=(run, name, command), name=f"praxbot-baseline-{name}", daemon=True
            )
            run.thread.start()

    def _run_baseline(self, run: BaselineRun, name: str, command: str) -> None:
        run.result = run_check(self.ws, name, command, self.settings.command_timeout, patch="")

    def _known_failures(self) -> set[str]:
        """Tests failing on the clean image, from the baseline runs that are back; never waits."""
        return {
            name
            for (clean, _), run in self._baselines.items()
            if clean == self.ws.clean and run.result is not None
            for name in run.result.failures
        }

    def _baseline(self, command: str, *, wait: bool) -> CheckResult | None:
        """The baseline run for a check, reported as an event the first time it is used. Without `wait`, only if it
        is already back."""
        run = self._baselines.get((self.ws.clean or "", command))
        if run is None:
            return None
        if run.thread is not None and wait:
            run.thread.join(self.settings.command_timeout + 60)
        if run.result is not None and not run.reported:
            run.reported = True
            self.on_event("verification.baseline", **run.result.as_dict())
        return run.result

    def _verify(self) -> None:
        self.checks = []
        self._expected_checks = self.check_commands
        if not self._expected_checks:
            return
        patch: str | None = None
        if self.ws.clean is not None:
            try:
                patch = self._applicable_changes().patch
            except (WorkspaceError, ValueError):
                patch = None  # without a diff to fork with, the checks run in the workspace as it stands
        if patch is not None:
            self._start_baselines()
        for name, command in self._expected_checks:
            if self.cancel.is_set():
                break
            self.on_event("verification.start", name=name, command=command)
            nothing_to_apply = patch is not None and not patch.strip()
            baseline = self._baseline(command, wait=nothing_to_apply) if patch is not None else None
            if nothing_to_apply and baseline is not None and baseline.error is None:
                result = dataclasses.replace(baseline, image=self.ws.image_id)  # the baseline run is the check
            else:
                result = run_check(self.ws, name, command, self.settings.command_timeout, patch=patch)
                if patch is not None:  # a slow suite's baseline may still be running: the two overlapped
                    baseline = self._baseline(command, wait=True)
            result = attributed(result, baseline)
            self.checks.append(result)
            self.on_event("verification.result", **result.as_dict())
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
                failures = "\n".join(
                    f"{c.command}: {c.attribution + chr(10) if c.attribution else ''}{c.output or c.error}"
                    for c in self.checks
                    if not c.passed
                )
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
        changes = self.ws.changes(paths) if paths is not None else self._applicable_changes()
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
        self._baselines.clear()  # the clean image moved with the apply; the old baseline runs speak for the old one
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
        self._baselines.clear()
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
            "checks": [check.as_dict() for check in self.checks],
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
