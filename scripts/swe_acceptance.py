"""Frozen, executable acceptance checks for the opt-in SWE-bench experiment."""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from prax_bot.core.agent import Agent, AgentResult
from prax_bot.core.prompts import prompt
from prax_bot.core.tool_result import ToolResult
from prax_bot.core.tools import ToolBox
from prax_bot.core.verification import CheckResult, run_check
from prax_bot.extensions.plugins import ExtraTool
from prax_bot.settings import Settings
from prax_bot.workspace.types import EventFn, Workspace

RESULT_MARKER = "PRAXBOT_ACCEPTANCE_RESULT:"
CheckStatus = Literal["passed", "failed", "error"]


class CheckSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: str = Field(min_length=1)
    setup: str
    assertions: tuple[str, ...] = Field(
        min_length=1,
        max_length=8,
        description="Python expressions returning bool; capture expected exceptions in setup and assert on variables",
    )


@dataclass(frozen=True)
class FrozenCheck:
    spec: CheckSpec
    command: str
    sha256: str


@dataclass(frozen=True)
class CheckObservation:
    status: CheckStatus
    execution: CheckResult
    details: dict | str


def build_check(spec: CheckSpec) -> FrozenCheck:
    """Validate syntax and freeze the entire check into an immutable inline command."""
    compile(spec.setup, "<acceptance-setup>", "exec")
    for index, expression in enumerate(spec.assertions):
        compile(expression, f"<acceptance-assertion-{index}>", "eval")
    snapshot = spec.model_copy(deep=True)
    payload = json.dumps(snapshot.model_dump(), sort_keys=True, separators=(",", ":"))
    # The task image can have Python 3.6. Keep the embedded wrapper compatible with it.
    script = f"""import json
import sys

_spec = json.loads({payload!r})
_scope = {{"__name__": "__prax_acceptance__"}}
_write = sys.stdout.write
_failed = []
_phase = "setup"
_index = None
try:
    exec(compile(_spec["setup"], "<acceptance-setup>", "exec"), _scope)
    _phase = "assertion"
    for _index, _expression in enumerate(_spec["assertions"]):
        _value = eval(compile(_expression, "<acceptance-assertion>", "eval"), _scope)
        if not isinstance(_value, bool):
            raise TypeError("Assertion must return bool, got " + type(_value).__name__)
        if _value is False:
            _failed.append(_index)
    _result = {{"status": "failed" if _failed else "passed", "failed": _failed}}
except BaseException as _error:
    _result = {{"status": "error", "phase": _phase, "index": _index,
               "error": type(_error).__name__ + ": " + str(_error)[:1000]}}
_write("\\n" + {RESULT_MARKER!r} + json.dumps(_result, sort_keys=True) + "\\n")
sys.exit({{"passed": 0, "failed": 1, "error": 2}}[_result["status"]])
"""
    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
    # Merge streams before run_check's output tail so setup stderr cannot hide the final marker.
    return FrozenCheck(snapshot, "python -c " + shlex.quote(script) + " 2>&1", digest)


def execute_check(ws: Workspace, frozen: FrozenCheck, name: str, timeout: int = 300) -> CheckObservation:
    """A false expression is check failure; exceptions and ambiguous results are errors."""
    execution = run_check(ws, name, frozen.command, timeout)
    if execution.error is not None or execution.timed_out or execution.exit_code is None:
        return CheckObservation("error", execution, execution.error or "Check timed out or did not exit")
    lines = execution.output.strip().splitlines()
    markers = [line for line in lines if line.startswith(RESULT_MARKER)]
    if len(markers) != 1 or not lines or lines[-1] != markers[0]:
        return CheckObservation("error", execution, "Missing, repeated, or nonfinal check result marker")
    try:
        details = json.loads(markers[0][len(RESULT_MARKER) :])
    except json.JSONDecodeError:
        return CheckObservation("error", execution, "Malformed check result JSON")
    if not isinstance(details, dict):
        return CheckObservation("error", execution, "Check result must be an object")
    status = details.get("status")
    codes = {"passed": 0, "failed": 1, "error": 2}
    if not isinstance(status, str) or status not in codes or execution.exit_code != codes[status]:
        return CheckObservation("error", execution, "Check status and process exit code do not agree")
    if status in ("passed", "failed"):
        failed = details.get("failed")
        if (
            not isinstance(failed, list)
            or any(type(index) is not int or not 0 <= index < len(frozen.spec.assertions) for index in failed)
            or (status == "failed") != bool(failed)
        ):
            return CheckObservation("error", execution, "Check result has no consistent assertion outcomes")
    return CheckObservation(status, execution, details)


def run_acceptance_experiment(
    llm,
    workspace: Workspace,
    *,
    author_workspace: Workspace,
    baseline_workspace: Workspace,
    task: str,
    output_dir: Path,
    settings: Settings,
    protected: Sequence[str],
    on_event: EventFn,
) -> tuple[AgentResult, dict]:
    """Author on a separate fork, execute frozen checks, and reserve one repair phase."""
    if settings.max_steps < 10:
        raise ValueError("acceptance experiments need at least 10 total tool turns")
    if workspace is author_workspace or workspace is baseline_workspace or author_workspace is baseline_workspace:
        raise ValueError("author, solver and original-baseline workspaces must be separate")
    author_budget = min(12, settings.max_steps // 4)
    repair_reserve = min(8, settings.max_steps // 5)
    record: dict = {"author_budget": author_budget, "repair_reserve": repair_reserve, "attempts": [], "phases": []}
    accepted: FrozenCheck | None = None
    original_result: CheckObservation | None = None

    def save() -> None:
        (output_dir / "acceptance.json").write_text(json.dumps(record, indent=2) + "\n")

    def phase_events(phase: str, offset: int = 0) -> EventFn:
        def emit(kind: str, **payload) -> None:
            # Checkpoint indices belong to each workspace. Model-turn indices span all phases.
            if "step" in payload and kind != "checkpoint":
                payload["phase_step"] = payload["step"]
                payload["step"] += offset
            on_event(kind, phase=phase, **payload)

        return emit

    def observe(frozen: FrozenCheck, ws: Workspace, phase: str) -> CheckObservation:
        observed = execute_check(ws, frozen, f"acceptance/{phase}", settings.command_timeout)
        on_event("acceptance.check", phase=phase, sha256=frozen.sha256, **asdict(observed))
        return observed

    def submit(_ws: Workspace, spec: CheckSpec) -> ToolResult:
        nonlocal accepted, original_result
        if accepted is not None:
            return ToolResult("error: the accepted check is frozen. Call finish now.", error=True)
        if len(record["attempts"]) >= 3:
            return ToolResult(
                "error: three check submissions have been evaluated. Finish with the blocker.", error=True
            )
        try:
            frozen = build_check(spec)
        except SyntaxError as error:
            record["attempts"].append({"spec": spec.model_dump(), "status": "error", "error": str(error)})
            save()
            return ToolResult(f"error: invalid Python in acceptance check: {error}", error=True)
        observed = observe(frozen, baseline_workspace, "baseline")
        record["attempts"].append({"spec": spec.model_dump(), "sha256": frozen.sha256, **asdict(observed)})
        if observed.status == "failed":
            accepted, original_result = frozen, observed
            record["accepted"] = {"spec": frozen.spec.model_dump(), "sha256": frozen.sha256, "command": frozen.command}
            (output_dir / "acceptance-command.sh").write_text(frozen.command + "\n")
            message = (
                "Accepted and frozen: at least one assertion is false on the original repository. "
                "This does not prove the check covers the full task. Call finish now."
            )
        else:
            message = (
                "Rejected: assertions already pass on the original repository; they do not expose this defect."
                if observed.status == "passed"
                else "Rejected: the check has an execution error, not an observed assertion failure."
            )
        save()
        return ToolResult(
            message + "\n" + observed.execution.output + "\n" + str(observed.details),
            error=observed.status != "failed",
        )

    def run_phase(phase: str, ws: Workspace, phase_task: str, budget: int, offset: int, plugins=()) -> AgentResult:
        events = phase_events(phase, offset)
        ws.on_event = events
        on_event("acceptance.phase", phase=phase, budget=budget, offset=offset)
        result = Agent(
            llm,
            ToolBox(ws, protected=protected, plugins=plugins),
            max_steps=budget,
            context_budget=settings.context_budget,
            on_event=events,
            investigate_model=settings.investigate_model,
            investigate_steps=settings.investigate_steps,
            verification_commands=(("acceptance", accepted.command),)
            if accepted is not None and phase != "author"
            else (),
        ).run(phase_task)
        record["phases"].append({"phase": phase, "budget": budget, "offset": offset, **asdict(result)})
        save()
        return result

    baseline_workspace.on_event = phase_events("baseline")
    author = run_phase(
        "author",
        author_workspace,
        prompt("acceptance_author", steps=author_budget, task=task),
        author_budget,
        0,
        (ExtraTool("submit_acceptance_check", prompt("acceptance_tool"), CheckSpec, submit),),
    )
    used = author.steps
    if author.reason == "cancelled":
        record.update(check_status="not run", total_steps=used)
        save()
        return author, record
    solver_task = task
    if accepted is not None and original_result is not None:
        solver_task += "\n\n" + prompt(
            "acceptance_solver",
            requirement=accepted.spec.requirement,
            setup=accepted.spec.setup,
            assertions="\n".join(f"- {expression}" for expression in accepted.spec.assertions),
            baseline_output=original_result.execution.output,
        )
    else:
        repair_reserve = 0
        record["check_status"] = "unavailable"
    result = run_phase("solver", workspace, solver_task, settings.max_steps - used - repair_reserve, used)
    used += result.steps
    if result.reason == "cancelled":
        record.update(check_status="not run", total_steps=used)
        save()
        return replace(result, steps=used), record
    if accepted is not None:
        observed = observe(accepted, workspace, "solver")
        record["solver_check"] = asdict(observed)
        save()
        if observed.status != "passed":
            repair_budget = min(repair_reserve, settings.max_steps - used)
            repair_task = (
                solver_task
                + "\n\n"
                + prompt(
                    "acceptance_repair",
                    steps=repair_budget,
                    summary=result.summary,
                    status=observed.status,
                    output=observed.execution.output + "\n" + str(observed.details),
                )
            )
            result = run_phase("repair", workspace, repair_task, repair_budget, used)
            used += result.steps
            if result.reason == "cancelled":
                record.update(check_status="not run", total_steps=used)
                save()
                return replace(result, steps=used), record
            observed = observe(accepted, workspace, "repair")
            record["repair_check"] = asdict(observed)
        record["check_status"] = observed.status
    record["total_steps"] = used
    save()
    return replace(result, steps=used), record
