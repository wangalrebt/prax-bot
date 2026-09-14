"""The agent loop: one model, a fixed tool set, a step budget."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

from prax_bot.core.context import (
    COMPACT_KEEP_TURNS,
    COMPACT_MIN_GAP,
    compact_old_tool_results,
    compact_transcript,
    prune_by_age,
)
from prax_bot.core.prompts import PACKAGED, prompt
from prax_bot.core.tools import TOOL_MODELS, ToolBox, tool_specs
from prax_bot.workspace.repository import SCRATCH_DIR
from prax_bot.workspace.types import EventFn, noop

DEFAULT_SYSTEM_PROMPT_PATH = Path(str(PACKAGED / "system.md"))
SYSTEM_PROMPT = prompt("system")  # the built-in prompt, or the user's override of it; see `prax prompt`

NUDGE = prompt("nudge")
MAX_NUDGES = 3  # consecutive turns without a tool call before finish is forced
LATE_FRACTION = 5  # the last fifth of the budget counts as late; the model is told to wrap up
LATE_NO_EDIT = prompt("late_no_edit")
LATE_EDITED = prompt("late_edited")
MID_FRACTION = 2  # halfway through the budget with nothing edited, the model is told to commit to a hypothesis
MID_NO_EDIT = prompt("mid_no_edit")
BLANK_RETRIES = 2  # a reply with no text and no tool call is retried before it counts as a stall
BLANK_NUDGE = prompt("nudge_blank")
REPEAT_NUDGE = prompt("nudge_repeat")
REPEAT_LIMIT = 3
REPEAT_WINDOW = 8
FORCE_FINISH = {"type": "function", "function": {"name": "finish"}}


@dataclass
class AgentResult:
    finished: bool
    summary: str
    steps: int
    reason: str
    goal_met: bool = False  # goal runs only: the model reported the whole objective achieved


def assistant_message(msg: Any) -> dict[str, Any]:
    """Rebuild the assistant turn explicitly so provider-specific extras are not echoed back."""
    out: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
    calls = getattr(msg, "tool_calls", None) or []
    if calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
            }
            for tc in calls
        ]
    return out


class Agent:
    def __init__(
        self,
        llm: Any,
        toolbox: ToolBox,
        *,
        max_steps: int = 40,
        max_transcript_chars: int = 160_000,
        on_event: EventFn | None = None,
        investigate_model: str | None = None,
        investigate_steps: int = 6,
        goal: bool = False,
        context_budget: int = 80_000,
        system_prompt: str | None = None,
        verification_commands: Sequence[tuple[str, str]] = (),
        cancel: Event | None = None,
    ) -> None:
        self.system_prompt = system_prompt or SYSTEM_PROMPT
        self.verification_commands = tuple(verification_commands)
        self.cancel = cancel or Event()
        self.steps = 0
        self.llm = llm
        self.context_budget = context_budget
        self.toolbox = toolbox
        self.investigate_model = investigate_model
        self.investigate_steps = investigate_steps
        self.max_steps = max_steps
        self.max_transcript_chars = max_transcript_chars
        self.on_event = on_event or noop
        self._goal = goal
        self.tools = self._specs()

    def system_message(self) -> str:
        """Engineering rules, actual workspace facts, and this round's verification plan."""
        workspace = self.toolbox.ws
        environment = prompt(f"environment/{workspace.backend}", workdir=workspace.workdir, scratch_dir=SCRATCH_DIR)
        verification = (
            prompt(
                "verification",
                commands="\n".join(f"- {name}: {command}" for name, command in self.verification_commands),
            )
            if self.verification_commands
            else prompt("verification_manual")
        )
        return "\n\n".join(
            part for part in (self.system_prompt, prompt("code_style"), environment, verification) if part
        )

    def _specs(self) -> list[dict[str, Any]]:
        specs = getattr(self.toolbox, "specs", None)
        return specs(goal=self._goal) if specs else tool_specs(goal=self._goal)

    def run(self, task: str) -> AgentResult:
        self.steps = 0
        if self.cancel.is_set():
            return self.cancelled()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_message()},
            {"role": "user", "content": task},
        ]
        instructions = getattr(self.toolbox, "project_instructions", None)
        if instructions is not None:
            try:
                text = instructions()
            except Exception:
                text = ""
            if text:
                messages.append({"role": "user", "content": text})
                self.on_event("project.instructions", chars=len(text))
        repo_map = getattr(self.toolbox, "repo_map", None)
        if repo_map is not None:
            try:
                overview = repo_map(task)
            except Exception as e:
                overview = ""
                self.on_event("repo.map", chars=0, error=f"{type(e).__name__}: {str(e)[:120]}")
            if overview:
                messages.append(
                    {
                        "role": "user",
                        "content": prompt("repo_map_intro") + "\n" + overview,
                    }
                )
                self.on_event("repo.map", chars=len(overview))
        skills_overview = getattr(self.toolbox, "skills_overview", None)
        if skills_overview is not None:
            try:
                skills_text = skills_overview()
            except Exception as e:
                skills_text = ""
                self.on_event("skills", count=0, error=f"{type(e).__name__}: {str(e)[:120]}")
            if skills_text:
                messages.append({"role": "user", "content": skills_text})
                self.on_event("skills", count=skills_text.count("\n- "))
            self.tools = self._specs()  # load_skill appears once skills are known
        call_names: dict[str, str] = {}
        compact = getattr(self.toolbox, "compact", None)
        nudges = 0
        recent_calls: list[str] = []
        step = 0  # model turns that called at least one tool; turns without a tool call do not count
        edited = False
        empty_finish_refused = False
        blank_retries = 0
        last_compacted_at = -COMPACT_MIN_GAP
        while True:
            if self.cancel.is_set():
                return self.cancelled()
            stalled = nudges >= MAX_NUDGES
            force_finish = step >= self.max_steps - 1 or stalled
            investigating = self.investigate_model and step < self.investigate_steps and not edited
            model = self.investigate_model if investigating else None
            context = int(getattr(self.llm, "last_prompt_tokens", 0) or 0)
            over_budget = bool(self.context_budget) and context > self.context_budget
            if over_budget and step >= 1 and step - last_compacted_at >= COMPACT_MIN_GAP:
                messages, summary, reason = compact_transcript(messages, self.llm)
                if summary:
                    last_compacted_at = step
                    self.on_event(
                        "context.compacted",
                        step=step,
                        tokens_before=context,
                        summary_chars=len(summary),
                        kept_turns=COMPACT_KEEP_TURNS,
                    )
                elif reason == "no summary":
                    last_compacted_at = step  # do not ask again every step
                    self.on_event(
                        "context.compacted",
                        step=step,
                        tokens_before=context,
                        summary_chars=0,
                        kept_turns=0,
                        error=reason,
                    )
                # "too short": nothing to compact yet; pruning below still applies
            prune_by_age(messages, names=call_names, compact=compact)
            compacted = compact_old_tool_results(
                messages, max_chars=self.max_transcript_chars, names=call_names, compact=compact
            )
            if compacted:
                self.on_event("context.elided", count=compacted)

            msg = self.llm.complete(
                messages, self.tools, tool_choice=FORCE_FINISH if force_finish else "auto", model=model
            )
            calls = getattr(msg, "tool_calls", None) or []
            finish_reason = getattr(self.llm, "last_finish_reason", None)
            self.on_event(
                "llm.call",
                step=step + 1,
                usage=self.llm.usage,
                model=model or getattr(self.llm, "model", ""),
                finish_reason=finish_reason,
                context=int(getattr(self.llm, "last_prompt_tokens", 0) or 0),
            )
            if self.cancel.is_set():
                return self.cancelled()

            if not calls and not (msg.content or "").strip() and blank_retries < BLANK_RETRIES:
                # nothing came back at all: a cut-off or dropped reply, not a decision; ask again
                blank_retries += 1
                if finish_reason == "length" and not (messages and messages[-1].get("content") == BLANK_NUDGE):
                    messages.append({"role": "user", "content": BLANK_NUDGE})
                self.on_event("nudge", step=step + 1, reason=f"blank reply (finish_reason={finish_reason}), retrying")
                continue
            blank_retries = 0
            messages.append(assistant_message(msg))

            if msg.content:
                self.on_event("assistant", step=step + 1, text=msg.content)
            if not calls:
                nudges += 1
                if force_finish:
                    return AgentResult(False, msg.content or "", step, "no finish call after forcing")
                messages.append({"role": "user", "content": NUDGE})
                self.on_event("nudge", step=step + 1, reason="the model replied without calling a tool")
                continue
            nudges = 0
            step += 1
            self.steps = step

            for tc in calls:
                if self.cancel.is_set():
                    return self.cancelled()
                name = tc.function.name
                raw_args = tc.function.arguments or "{}"
                self.on_event("tool.call", step=step, name=name, args=raw_args)
                if name == "finish":
                    if stalled and not _has_summary(raw_args) and step < self.max_steps - 1:
                        # forced after a stall, but the model sent another tool's arguments: it wants to keep working
                        nudges = 0
                        output = "error: finish needs a summary. Continue with the tool you meant to call."
                        self.on_event("tool.result", step=step, name=name, output=output, error=True)
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})
                        call_names[tc.id] = name
                        continue
                    if not edited and not empty_finish_refused and not force_finish:
                        empty_finish_refused = True
                        output = prompt("finish_without_edits")
                        self.on_event("tool.result", step=step, name=name, output=output, error=True)
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})
                        call_names[tc.id] = name
                        continue
                    summary = _finish_summary(raw_args)
                    goal_met = _finish_flag(raw_args, "goal_met")
                    self.on_event("finish", step=step, summary=summary, goal_met=goal_met)
                    return AgentResult(True, summary, step, "finish", goal_met)
                tool_result = self.toolbox.call(name, raw_args)
                if name in ("edit_file", "write_file") and not tool_result.error:
                    edited = True
                if name == "todo_write":
                    self.on_event("todo", step=step, todos=list(getattr(self.toolbox, "todos", [])))
                output = f"{tool_result.output}\n(step {step} of {self.max_steps})"
                left = self.max_steps - step
                repo_edited = getattr(self.toolbox, "repo_edited", edited)
                if 0 < left <= max(3, self.max_steps // LATE_FRACTION):
                    output += " " + (LATE_EDITED if repo_edited else LATE_NO_EDIT).format(left=left)
                elif step == self.max_steps // MID_FRACTION and not repo_edited and self.max_steps >= 10:
                    output += " " + MID_NO_EDIT.format(left=left)
                self.on_event("tool.result", step=step, name=name, output=output, error=tool_result.error)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})
                call_names[tc.id] = name
                recent_calls = [*recent_calls, call_signature(name, raw_args)][-REPEAT_WINDOW:]

            repeated = recent_calls and recent_calls.count(recent_calls[-1]) >= REPEAT_LIMIT
            if repeated:
                messages.append({"role": "user", "content": REPEAT_NUDGE})
                self.on_event("nudge", step=step, reason="the same call was repeated three times")
                recent_calls = [c for c in recent_calls if c != recent_calls[-1]]

            if step >= self.max_steps:
                return AgentResult(False, "step budget exhausted before finish was called", step, "budget")

    def cancelled(self) -> AgentResult:
        return AgentResult(False, "stopped by the user; completed tool calls are preserved", self.steps, "cancelled")


def call_signature(name: str, raw_args: str) -> str:
    """A canonical form of a tool call, so repeats are detected regardless of key order or omitted defaults."""
    model = TOOL_MODELS.get(name)
    try:
        args = json.loads(raw_args) if raw_args else {}
        if model is not None and isinstance(args, dict):
            args = model.model_validate(args).model_dump()
        return f"{name}:{json.dumps(args, sort_keys=True)}"
    except Exception:
        return f"{name}:{raw_args}"


def _has_summary(raw_args: str) -> bool:
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        return False
    return isinstance(args, dict) and bool(args.get("summary"))


def _finish_flag(raw_args: str, name: str) -> bool:
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        return False
    return isinstance(args, dict) and args.get(name) is True


def _finish_summary(raw_args: str) -> str:
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        return raw_args.strip() or "(no summary given)"
    if isinstance(args, dict) and args.get("summary"):
        return str(args["summary"])
    return "(no summary given)"
