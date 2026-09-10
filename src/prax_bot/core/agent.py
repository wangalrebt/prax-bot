"""The agent loop: one model, a fixed tool set, a step budget."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prax_bot.core.prompts import PACKAGED, prompt
from prax_bot.core.tools import TOOL_MODELS, ToolBox, tool_specs

EventFn = Callable[..., None]


DEFAULT_SYSTEM_PROMPT_PATH = Path(str(PACKAGED / "system.md"))
SYSTEM_PROMPT = prompt("system")  # the built-in prompt, or the user's override of it; see `prax prompt`

NUDGE = prompt("nudge")
MAX_NUDGES = 3  # consecutive turns without a tool call before finish is forced
KEEP_FULL = 6  # the most recent tool results stay complete
PRUNE_TIERS = ((12, 4096), (24, 1024))  # older results are cut to these sizes; beyond the last tier, a one-line digest
COMPACT_KEEP_TURNS = 3  # assistant turns kept verbatim after a compaction
COMPACT_MIN_GAP = 6  # steps between two compactions
SUMMARY_SYSTEM = prompt("summary")
LATE_FRACTION = 5  # the last fifth of the budget counts as late; the model is told to wrap up
LATE_NO_EDIT = prompt("late_no_edit")
LATE_EDITED = prompt("late_edited")
BLANK_RETRIES = 2  # a reply with no text and no tool call is retried before it counts as a stall
BLANK_NUDGE = prompt("nudge_blank")
REPEAT_NUDGE = prompt("nudge_repeat")
REPEAT_LIMIT = 3
REPEAT_WINDOW = 8
FORCE_FINISH = {"type": "function", "function": {"name": "finish"}}


def _noop(kind: str, **payload: Any) -> None:
    return None


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


def compact_old_tool_results(
    messages: list[dict[str, Any]],
    *,
    max_chars: int,
    keep_last: int = 6,
    names: dict[str, str] | None = None,
    compact: Callable[[str, str], str] | None = None,
) -> int:
    """Shrink the oldest tool outputs until the transcript fits. Returns how many were compacted.

    With a `compact` function the output is replaced by a short summary that keeps what it was
    about; without one it is replaced by a stub.
    """
    total = sum(len(str(m.get("content") or "")) for m in messages)
    if total <= max_chars:
        return 0
    tool_indexes = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    done = 0
    for i in tool_indexes[:-keep_last] if keep_last else tool_indexes:
        content = messages[i].get("content") or ""
        if content.startswith(("[compacted]", "[elided")):
            continue
        name = (names or {}).get(messages[i].get("tool_call_id", ""), "")
        if compact is not None and name:
            replacement = compact(name, content)
        else:
            replacement = f"[elided earlier tool output of {len(content)} characters]"
        messages[i]["content"] = replacement
        total -= len(content) - len(replacement)
        done += 1
        if total <= max_chars:
            break
    return done


elide_old_tool_results = compact_old_tool_results  # older name


def _cut(text: str, cap: int) -> str:
    """The head and tail of an oversized result with a marker between, fitting inside `cap` so a second pass is a no-op."""
    if len(text) <= cap:
        return text
    marker = f"\n[… pruned {len(text):,} characters of an older result to its head and tail …]\n"
    room = max(cap - len(marker), 40)
    head = int(room * 0.75)
    tail = room - head
    return f"{text[:head]}{marker}{text[-tail:]}"


def prune_by_age(
    messages: list[dict[str, Any]],
    *,
    names: dict[str, str] | None = None,
    compact: Callable[[str, str], str] | None = None,
) -> int:
    """Shrink tool results as they age: the newest stay whole, older ones lose their middle, the oldest become a digest.

    Runs every step, so the context carries recent detail and only a memory of the rest. Returns how many changed.
    """
    tool_indexes = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    changed = 0
    for age, i in enumerate(reversed(tool_indexes)):
        if age < KEEP_FULL:
            continue
        content = messages[i].get("content") or ""
        if content.startswith(("[compacted]", "[elided")):
            continue
        cap = next((size for limit, size in PRUNE_TIERS if age < limit), None)
        if cap is None:
            name = (names or {}).get(messages[i].get("tool_call_id", ""), "")
            replacement = (
                compact(name, content)
                if compact and name
                else f"[elided earlier tool output of {len(content)} characters]"
            )
        elif len(content) > cap:
            replacement = _cut(content, cap)
        else:
            continue
        if replacement != content:
            messages[i]["content"] = replacement
            changed += 1
    return changed


def render_for_summary(messages: list[dict[str, Any]], limit: int = 120_000) -> str:
    """The transcript as plain text for the summariser: roles, tool calls with their arguments, results trimmed."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "assistant":
            text = (m.get("content") or "").strip()
            calls = m.get("tool_calls") or []
            parts = [text] if text else []
            for tc in calls:
                fn = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", None)
                name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", "")
                args = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", "")
                parts.append(f"call {name}({str(args)[:300]})")
            lines.append("assistant: " + " | ".join(parts))
        elif role == "tool":
            lines.append("result: " + str(m.get("content") or "")[:600].replace("\n", " "))
        else:
            lines.append("user: " + str(m.get("content") or "")[:2000])
    text = "\n".join(lines)
    return _cut(text, limit)


def compact_transcript(
    messages: list[dict[str, Any]], llm: Any, *, keep_turns: int = COMPACT_KEEP_TURNS
) -> tuple[list[dict[str, Any]], str, str]:
    """Replace the middle of a long transcript with a model-written summary.

    Kept verbatim: the system prompt and the opening user messages except the repository map, and the last
    `keep_turns` assistant turns with their tool results, so the model's recent context is exact. Returns the
    messages, the summary, and a reason when nothing was done: "too short" (nothing to compact yet) or
    "no summary" (the model gave none, or the call failed); the transcript is then returned unchanged.
    """
    assistant_indexes = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    if len(assistant_indexes) <= keep_turns:
        return messages, "", "too short"
    first_assistant = assistant_indexes[0]
    tail_start = assistant_indexes[-keep_turns]
    try:
        reply = llm.complete(
            [
                {"role": "system", "content": SUMMARY_SYSTEM},
                {"role": "user", "content": render_for_summary(messages[:tail_start])},
            ],
            None,
        )
        summary = (getattr(reply, "content", None) or "").strip()
    except Exception:
        summary = ""
    if not summary:
        return messages, "", "no summary"
    preamble = [m for m in messages[:first_assistant] if not str(m.get("content") or "").startswith("Repository map")]
    bridge = {"role": "user", "content": prompt("compacted_bridge", summary=summary)}
    return [*preamble, bridge, *messages[tail_start:]], summary, ""


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
    ) -> None:
        self.system_prompt = system_prompt or SYSTEM_PROMPT
        self.llm = llm
        self.context_budget = context_budget
        self.toolbox = toolbox
        self.investigate_model = investigate_model
        self.investigate_steps = investigate_steps
        self.max_steps = max_steps
        self.max_transcript_chars = max_transcript_chars
        self.on_event = on_event or _noop
        self._goal = goal
        self.tools = self._specs()

    def _specs(self) -> list[dict[str, Any]]:
        specs = getattr(self.toolbox, "specs", None)
        return specs(goal=self._goal) if specs else tool_specs(goal=self._goal)

    def run(self, task: str) -> AgentResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
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

            for tc in calls:
                name = tc.function.name
                raw_args = tc.function.arguments or "{}"
                self.on_event("tool.call", step=step, name=name, args=raw_args)
                if name == "finish":
                    if stalled and not _has_summary(raw_args) and step < self.max_steps - 1:
                        # forced after a stall, but the model sent another tool's arguments: it wants to keep working
                        nudges = 0
                        output = "error: finish needs a summary. Continue with the tool you meant to call."
                        self.on_event("tool.result", step=step, name=name, output=output)
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})
                        call_names[tc.id] = name
                        continue
                    if not edited and not empty_finish_refused and not force_finish:
                        empty_finish_refused = True
                        output = (
                            "error: no changes have been made to the repository. If the task cannot be done, call finish "
                            "again and explain why in the summary; otherwise keep working."
                        )
                        self.on_event("tool.result", step=step, name=name, output=output)
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})
                        call_names[tc.id] = name
                        continue
                    summary = _finish_summary(raw_args)
                    goal_met = _finish_flag(raw_args, "goal_met")
                    self.on_event("finish", step=step, summary=summary, goal_met=goal_met)
                    return AgentResult(True, summary, step, "finish", goal_met)
                output = self.toolbox.call(name, raw_args)
                if name in ("edit_file", "write_file"):
                    edited = True
                if name == "todo_write":
                    self.on_event("todo", step=step, todos=list(getattr(self.toolbox, "todos", [])))
                output = f"{output}\n(step {step} of {self.max_steps})"
                left = self.max_steps - step
                if 0 < left <= max(3, self.max_steps // LATE_FRACTION):
                    repo_edited = getattr(self.toolbox, "repo_edited", edited)
                    output += " " + (LATE_EDITED if repo_edited else LATE_NO_EDIT).format(left=left)
                self.on_event("tool.result", step=step, name=name, output=output)
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
