"""Session execution and state shared by every interface.

The runtime emits events and returns results. Terminal prompts, slash-command parsing,
and rendering belong to the interfaces.
"""

from __future__ import annotations

import copy
import dataclasses
import time
import traceback
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event, RLock, Thread
from typing import TYPE_CHECKING, Any, TextIO

from prax_bot.core.agent import Agent, AgentResult
from prax_bot.core.goal import GoalDriver, goal_task
from prax_bot.core.llm import Usage
from prax_bot.core.tools import ToolBox
from prax_bot.core.verification import CheckResult, VerificationStatus, attributed, run_check, verification_status
from prax_bot.extensions.plugins import ExtraTool
from prax_bot.extensions.skills import Skill
from prax_bot.runtime.apply import ApplyTransaction, repository_apply_lock
from prax_bot.runtime.runs import (
    Run,
    build_record,
    continuation_task,
    load_run,
    new_run,
    reopen_workspace,
    write_record,
)
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


def _saved_check(data: dict[str, Any]) -> CheckResult:
    """Rehydrate persisted evidence when selecting a candidate after a restart."""
    values = {field.name: data[field.name] for field in dataclasses.fields(CheckResult) if field.name in data}
    values["baseline"] = _saved_check(data["baseline"]) if data.get("baseline") else None
    for key in ("failures", "new_failures", "known_failures"):
        values[key] = tuple(values.get(key, ()))
    return CheckResult(**values)


def _record_tool_event(trajectory: list[dict[str, Any]], kind: str, payload: dict[str, Any]) -> None:
    """Pair each candidate tool call with its full output, both live and when recovering its log."""
    if kind == "tool.call":
        trajectory.append(
            {
                "step": payload.get("step"),
                "name": payload.get("name"),
                "args": payload.get("args", ""),
                "result": "",
            }
        )
    elif kind == "tool.result" and trajectory:
        trajectory[-1].update(result=str(payload.get("output") or ""), error=payload.get("error", False))


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
        self.confirm_no_edit_finish = True
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
        self._workspace_id = uuid.uuid4().hex
        self.comparisons: list[dict[str, Any]] = []
        self._comparison_children: dict[str, Session] = {}
        self._comparison_lock = RLock()
        self._retired_cost = 0.0
        self._retired_commands = 0
        self.ws.on_event = self.on_event
        self.record()

    def on_event(self, kind: str, **payload: Any) -> None:
        self._observer(kind, **payload)
        if kind == "checkpoint":
            self.record()

    def request_stop(self) -> None:
        """Signal the worker; the current model or tool call is allowed to finish."""
        self.cancel.set()
        with self._comparison_lock:
            for child in self._comparison_children.values():
                child.request_stop()
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
        # Both candidate workers report here; an older snapshot must not overwrite newer evidence.
        with self._comparison_lock:
            self._write_record()

    def _write_record(self) -> None:
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
                "expected": [list(command) for command in self._expected_checks or self.check_commands],
                "checks": [check.as_dict() for check in self.checks],
            },
            "test_command": self.settings.test_command,
            "lint_command": self.settings.lint_command,
            "context_budget": self.settings.context_budget,
            "workspace_id": self._workspace_id,
            "comparisons": self._saved_comparisons(),
            **self._totals(),
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
            confirm_no_edit_finish=self.confirm_no_edit_finish,
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
            totals = self._totals()
            self.on_event(
                "session.round",
                result=result,
                steps_total=totals["steps"],
                round_steps=result.steps,
                elapsed=time.monotonic() - self.started,
                usage=Usage(
                    totals["prompt_tokens"],
                    totals["completion_tokens"],
                    totals["model_calls"],
                    totals["usage_by_model"],
                ),
                commands=totals["sandbox_commands"],
                checkpoints=len(self.ws.checkpoints),
                cost=totals["sandbox_cost"],
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
        if clean is None or self.cancel.is_set():
            return
        for name, command in self.check_commands:
            if self.cancel.is_set():
                break
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
        with repository_apply_lock(self.root):
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
        self._retired_cost += self.ws.total_cost
        self._retired_commands += self.ws.commands
        self.ws = workspace
        self._workspace_id = uuid.uuid4().hex
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
        self.restore_comparisons(past, step)
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

    def _saved_comparisons(self) -> list[dict[str, Any]]:
        with self._comparison_lock:
            return copy.deepcopy(self.comparisons)

    def restore_continuity(self, past: Run, step: int, *, pending_apply: bool) -> None:
        """Continue one logical session's accounting and evidence in a fresh run segment."""
        info = past.info
        saved_session = info.get("session") or {}
        self.rounds = int(saved_session.get("rounds", 0))
        self.applied_rounds = int(saved_session.get("applied_rounds", 0))
        self.has_applied = bool(info.get("applied"))
        self.goal_info = copy.deepcopy(info.get("goal"))
        self.started -= float(info.get("elapsed_s", 0))

        # The parent's persisted totals already include its candidates. Seed only its own share;
        # restore_comparisons may then recover newer child totals from an interrupted comparison.
        keys = ("steps", "prompt_tokens", "completion_tokens", "model_calls", "sandbox_commands", "sandbox_cost")
        own = {key: info.get(key, 0) for key in keys}
        by_model = copy.deepcopy(info.get("usage_by_model") or {})
        for comparison in info.get("comparisons") or []:
            for candidate in comparison["candidates"]:
                for key in keys:
                    own[key] = max(0, own[key] - candidate.get(key, 0))
                for model, counts in candidate.get("usage_by_model", {}).items():
                    for key, value in counts.items():
                        if model in by_model and key in by_model[model]:
                            by_model[model][key] = max(0, by_model[model][key] - value)
        self.steps = int(own["steps"])
        self.llm.usage = Usage(
            int(own["prompt_tokens"]), int(own["completion_tokens"]), int(own["model_calls"]), by_model
        )
        self._retired_commands = int(own["sandbox_commands"])
        self._retired_cost = float(own["sandbox_cost"])

        evidence = info.get("verification") or {}
        expected = evidence.get("expected")
        saved_expected = (
            tuple((name, command) for name, command in expected)
            if expected is not None
            else tuple(
                (name, info[key])
                for name, key in (("tests", "test_command"), ("lint", "lint_command"))
                if info.get(key)
            )
        )
        # Logs retain the old commands, but a changed configured check must be satisfied too.
        # A saved goal check is evidence for that goal, independent of current test/lint settings.
        self._expected_checks = self.check_commands + tuple(
            (name, command) for name, command in saved_expected if name not in ("tests", "lint")
        )
        exact = step == len(past.checkpoints) - 1 and not pending_apply
        source_image = past.checkpoints[step]["image"]
        self.checks = []
        for data in evidence.get("checks") or []:
            check = _saved_check(data)
            image = (
                self.ws.image_id
                if exact and check.image == source_image
                else check.image
                if check.image.startswith("saved:")
                else f"saved:{past.run_id}:{check.image}"
            )
            self.checks.append(dataclasses.replace(check, image=image))

    def _totals(self) -> dict[str, Any]:
        """Count this session and both candidates once, including after choosing a workspace."""
        usage = self.llm.usage
        totals = {
            "steps": self.steps + (self._agent.steps if self._agent else 0),
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "model_calls": usage.calls,
            "sandbox_commands": self.ws.commands + self._retired_commands,
            "sandbox_cost": self.ws.total_cost + self._retired_cost,
            "usage_by_model": copy.deepcopy(usage.by_model),
        }
        for comparison in self._saved_comparisons():
            for candidate in comparison["candidates"]:
                for key in (
                    "steps",
                    "prompt_tokens",
                    "completion_tokens",
                    "model_calls",
                    "sandbox_commands",
                    "sandbox_cost",
                ):
                    totals[key] += candidate.get(key, 0)
                for model, counts in candidate.get("usage_by_model", {}).items():
                    target = totals["usage_by_model"].setdefault(model, {})
                    for key, value in counts.items():
                        target[key] = target.get(key, 0) + value
        return totals

    def _comparison_source(self) -> dict[str, Any]:
        return {
            "workspace_id": self._workspace_id,
            "checkpoint": self.ws.current_step,
            "image": self.ws.image_id,
            "clean": self.ws.clean,
        }

    def comparison_state(self) -> dict[str, Any] | None:
        comparisons = self._saved_comparisons()
        if not comparisons:
            return None
        comparison = comparisons[-1]
        comparison["stale"] = comparison["selected"] is None and comparison["source"] != self._comparison_source()
        return comparison

    def compare(self, objective: str, *, approach_a: str = "", approach_b: str = "") -> dict[str, Any]:
        """Run two independent attempts from one checkpoint, leaving the active workspace alone."""
        self._require_ready()
        if not objective.strip():
            raise ValueError("a comparison needs an objective")
        self.diff()
        self.active = True
        comparison = {
            "id": uuid.uuid4().hex,
            "objective": objective.strip(),
            "source": self._comparison_source(),
            "status": "running",
            "selected": None,
            "candidates": [],
        }
        self.comparisons.append(comparison)
        opened: list[tuple[dict[str, Any], Path, TextIO, EventFn]] = []
        threads: list[Thread] = []
        try:
            for label, approach in (("A", approach_a), ("B", approach_b)):
                candidate: dict[str, Any] = {
                    "label": label,
                    "approach": approach.strip(),
                    "status": "starting",
                    "summary": "",
                    "steps": 0,
                    "tokens": 0,
                    "calls": 0,
                    "cost": 0.0,
                    "verification": "not run",
                    "checks": [],
                    "diff": "",
                    "diff_error": None,
                    "error": None,
                    "trajectory": [],
                }
                run_id, directory, events, emit = new_run(
                    self.settings,
                    lambda kind, _candidate=candidate, **payload: self._comparison_event(
                        comparison, _candidate, kind, payload
                    ),
                )
                candidate["run_id"] = run_id
                comparison["candidates"].append(candidate)
                opened.append((candidate, directory, events, emit))
            self.record()  # both run ids exist before either worker starts
            source = load_run(self.run_id, self.settings)
            self.on_event("comparison.start", comparison_id=comparison["id"])
            for candidate, directory, events, emit in opened:
                thread = Thread(
                    target=self._run_candidate,
                    args=(comparison, candidate, source, directory, events, emit),
                    name=f"praxbot-candidate-{candidate['label']}",
                    daemon=True,
                )
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join()
            comparison["status"] = "stopped" if self.cancel.is_set() else "complete"
        except BaseException:
            self.request_stop()
            for thread in threads:
                if thread.ident is not None:
                    thread.join()
            comparison["status"] = "interrupted"
            raise
        finally:
            for _, _, events, _ in opened:
                events.close()
            with self._comparison_lock:
                self._comparison_children.clear()
            self.active = False
            self.cancel.clear()
            self.record()
            self.on_event("comparison.done", comparison_id=comparison["id"])
        return self.comparison_state() or comparison

    def _comparison_event(
        self, comparison: dict[str, Any], candidate: dict[str, Any], kind: str, payload: dict[str, Any]
    ) -> None:
        with self._comparison_lock:
            _record_tool_event(candidate["trajectory"], kind, payload)
            child = self._comparison_children.get(candidate["label"])
            if child is not None:
                self._update_candidate(candidate, child)
        self.on_event(
            "comparison.event",
            comparison_id=comparison["id"],
            candidate=candidate["label"],
            event_kind=kind,
            event=payload,
        )
        if kind in ("tool.call", "tool.result", "verification.result", "session.round"):
            self.record()
            self.on_event("comparison.updated", comparison_id=comparison["id"])

    def _update_candidate(self, candidate: dict[str, Any], child: Session) -> None:
        totals = child._totals()
        candidate.update(
            **totals,
            tokens=totals["prompt_tokens"] + totals["completion_tokens"],
            calls=totals["model_calls"],
            cost=totals["sandbox_cost"],
            summary=child.last_summary,
            verification=child.verification_status,
            checks=[check.as_dict() for check in child.checks],
            expected_checks=list(child._expected_checks or child.check_commands),
            diff=child.last_diff,
            diff_error=child.diff_error,
            checkpoint=child.ws.current_step,
            image=child.ws.image_id,
            clean=child.ws.clean,
        )

    def _run_candidate(
        self,
        comparison: dict[str, Any],
        candidate: dict[str, Any],
        source: Run,
        directory: Path,
        events: TextIO,
        emit: EventFn,
    ) -> None:
        child: Session | None = None
        status = "stopped"
        try:
            if self.cancel.is_set():
                with self._comparison_lock:
                    candidate["status"] = "stopped"
                return
            workspace = reopen_workspace(
                source, comparison["source"]["checkpoint"], self.settings, emit, candidate["run_id"]
            )
            child = Session(
                self.settings,
                workspace,
                self.root,
                backend=self.backend,
                run_id=candidate["run_id"],
                run_dir=directory,
                events_file=events,
                on_event=emit,
                protect=self.protect,
                skills=self.skills,
                plugins=tuple(plugin for plugin in self.plugins if plugin.source != "main-session"),
                kind="comparison-candidate",
            )
            child.resumed_from = {
                "run_id": source.run_id,
                "step": comparison["source"]["checkpoint"],
                "image": comparison["source"]["image"],
            }
            with self._comparison_lock:
                self._comparison_children[candidate["label"]] = child
                if self.cancel.is_set():
                    candidate["status"] = "stopped"
                    return
                candidate["status"] = "working"
            instruction = comparison["objective"]
            if candidate["approach"]:
                instruction += "\nApproach to explore: " + candidate["approach"]
            else:
                instruction += (
                    "\nDevelop an independent solution, using the repository evidence to choose your approach."
                )
            task = (
                continuation_task(self.first_task, self.last_summary, self.last_diff, instruction)
                if self.first_task
                else instruction
            )
            child.run_round(task, original_task=comparison["objective"])
            reason = child.last_result.reason if child.last_result else "error"
            status = "error" if reason == "error" else "complete" if reason == "finish" else "stopped"
        except Exception as error:
            status = "error"
            with self._comparison_lock:
                candidate["error"] = f"{type(error).__name__}: {str(error)[:500]}"
        finally:
            if child is not None:
                try:
                    # Baselines belong to the child too; drain them before closing its event writer or counting cost.
                    for baseline in child._baselines.values():
                        if baseline.thread is not None:
                            baseline.thread.join()
                    child.close()
                except Exception as error:
                    status = "error"
                    with self._comparison_lock:
                        candidate["error"] = f"{type(error).__name__}: {str(error)[:500]}"
                with self._comparison_lock:
                    self._update_candidate(candidate, child)
                    candidate["status"] = status
                    self._comparison_children.pop(candidate["label"], None)
            else:
                with self._comparison_lock:
                    candidate["status"] = status
                write_record(
                    directory,
                    {
                        "run_id": candidate["run_id"],
                        "kind": "comparison-candidate",
                        "task": comparison["objective"],
                        "repo": str(self.root),
                        "backend": self.backend,
                        "model": self.settings.model,
                        "state": "closed",
                        "activity_tracking": "event_lock",
                        "finished": False,
                        "reason": "cancelled" if candidate["status"] == "stopped" else "error",
                        "summary": candidate["error"] or "Stopped before the candidate workspace was ready.",
                        "checkpoints": [],
                        "steps": 0,
                        "model_calls": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "resumed_from": {"run_id": source.run_id, "step": comparison["source"]["checkpoint"]},
                    },
                )
            events.close()
            self.record()
            self.on_event("comparison.updated", comparison_id=comparison["id"])

    def restore_comparisons(self, past: Run, step: int) -> None:
        """Keep saved candidates reviewable on resume; never restart their agents implicitly."""
        imported = copy.deepcopy(past.info.get("comparisons") or [])
        if not imported:
            return
        # A /resume changes the reviewed workspace, not work already spent in this session.
        imported_ids = {item["id"] for item in imported}
        self.comparisons = [item for item in self.comparisons if item["id"] not in imported_ids] + imported
        latest = self.comparisons[-1]
        source = latest["source"]
        if (
            past.pending_apply is None
            and source["workspace_id"] == past.info.get("workspace_id")
            and source["checkpoint"] == step
            and source["image"] == past.checkpoints[step]["image"]
            and source["clean"] == past.checkpoints[step].get("clean")
        ):
            latest["source"] = self._comparison_source()
        if latest["status"] == "running":
            latest["status"] = "interrupted"
        for candidate in latest["candidates"]:
            if candidate["status"] in ("starting", "working"):
                candidate["status"] = "interrupted"
                try:
                    saved = load_run(candidate["run_id"], self.settings)
                except (OSError, ValueError):
                    continue
                info = saved.info
                if not saved.checkpoints:
                    continue
                checkpoint = saved.checkpoints[-1]
                verification = info.get("verification") or {}
                trajectory: list[dict[str, Any]] = []
                for event in saved.events:
                    _record_tool_event(trajectory, event.get("kind", ""), event)
                for key in (
                    "steps",
                    "prompt_tokens",
                    "completion_tokens",
                    "model_calls",
                    "sandbox_commands",
                    "sandbox_cost",
                    "usage_by_model",
                ):
                    if key in info:
                        candidate[key] = info[key]
                candidate.update(
                    tokens=info.get("prompt_tokens", 0) + info.get("completion_tokens", 0),
                    calls=info.get("model_calls", 0),
                    cost=info.get("sandbox_cost", 0),
                    summary=info.get("summary", ""),
                    checks=verification.get("checks", []),
                    verification=verification.get("status", "not run"),
                    expected_checks=[
                        (name, info[key])
                        for name, key in (("tests", "test_command"), ("lint", "lint_command"))
                        if info.get(key)
                    ],
                    diff=saved.diff,
                    diff_error=info.get("diff_error"),
                    trajectory=trajectory,
                    checkpoint=checkpoint["step"],
                    image=checkpoint["image"],
                    clean=checkpoint.get("clean"),
                )
                if info.get("state") == "closed":
                    candidate["status"] = (
                        "complete" if info.get("finished") else "error" if info.get("reason") == "error" else "stopped"
                    )

    def choose_candidate(self, label: str, *, comparison_id: str | None = None) -> None:
        """Choose the pending workspace; applying it to the host remains a separate action."""
        self._require_ready()
        comparison = self.comparison_state()
        if comparison is None or (comparison_id is not None and comparison_id != comparison["id"]):
            raise ValueError("that comparison is no longer current")
        if comparison["selected"] is not None:
            raise ValueError("a candidate has already been chosen")
        if comparison["stale"]:
            raise ValueError("the workspace changed since this comparison; start a new comparison")
        candidate = next((item for item in comparison["candidates"] if item["label"] == label.upper()), None)
        if candidate is None or candidate["status"] not in ("complete", "stopped") or "checkpoint" not in candidate:
            raise ValueError("choose a completed candidate: A or B")
        if candidate["diff_error"]:
            raise WorkspaceError("the candidate's diff is unavailable")
        past = load_run(candidate["run_id"], self.settings)
        step = candidate["checkpoint"]
        if (
            past.info.get("repo") != str(self.root)
            or past.info.get("backend") != self.backend
            or past.pending_apply is not None
            or len(past.checkpoints) != step + 1
            or past.checkpoints[step]["image"] != candidate["image"]
            or past.checkpoints[step].get("clean") != candidate["clean"]
        ):
            raise ValueError("the saved candidate changed; it cannot be selected")
        workspace = reopen_workspace(
            past, step, self.settings, self._observer, f"{self.run_id}-choice-{uuid.uuid4().hex[:8]}"
        )
        changes = workspace.changes()
        if changes.patch != candidate["diff"]:
            raise ValueError("the saved candidate patch changed; it cannot be selected")
        self._retired_cost += self.ws.total_cost
        self._retired_commands += self.ws.commands
        self.ws = workspace
        self._workspace_id = uuid.uuid4().hex
        workspace.on_event = self.on_event
        self.first_task = self.first_task or comparison["objective"]
        self.instructions.append(f"Selected candidate {candidate['label']}: {comparison['objective']}")
        self.last_summary = candidate["summary"]
        self.last_result = None
        self.goal_info = None
        self._round_states.clear()
        self._baselines.clear()
        self._expected_checks = tuple(tuple(item) for item in candidate["expected_checks"])
        self.checks = [
            dataclasses.replace(_saved_check(item), image=workspace.image_id) for item in candidate["checks"]
        ]
        self.changes, self.last_diff, self.diff_error = changes, changes.patch, None
        (self.run_dir / "diff.patch").write_text(self.last_diff, encoding="utf-8")
        self.comparisons[-1]["selected"] = candidate["label"]
        self.record()
        self.on_event("comparison.selected", comparison_id=comparison["id"], candidate=candidate["label"])

    def state(self) -> dict[str, Any]:
        totals = self._totals()
        return {
            "run_id": self.run_id,
            "repo": str(self.root),
            "name": self.root.name,
            "backend": self.backend,
            "model": self.settings.model,
            "effort": self.settings.effort,
            "rounds": self.rounds,
            "steps": totals["steps"],
            "tokens": totals["prompt_tokens"] + totals["completion_tokens"],
            "context": int(getattr(self.llm, "last_prompt_tokens", 0) or 0),
            "context_budget": self.settings.context_budget,
            "calls": totals["model_calls"],
            "checkpoints": len(self.ws.checkpoints),
            "cost": totals["sandbox_cost"],
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
            "comparison": self.comparison_state(),
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
