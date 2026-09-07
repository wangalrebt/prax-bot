"""The agent loop: one model, a fixed tool set, a step budget."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .tools import TOOL_MODELS, ToolBox, tool_specs

EventFn = Callable[..., None]

SYSTEM_PROMPT = """You are Prax Bot, a careful software engineer working inside an isolated Linux sandbox.
The repository is at /work, which is the working directory for every command. Nothing you do touches the user's machine: when you finish, the user is shown a diff and asked whether to apply it.

Rules:
- Inspect before editing: list, search, and read the relevant files first. In large files, call outline to see what the file defines, then read only the relevant region with start_line and end_line. Never guess function names; look them up.
- Make the smallest change that completes the task. Do not refactor or reformat unrelated code.
- Use edit_file for changes to existing files and write_file only for new files. If edit_file reports that the text was not found, copy the exact lines from the region it shows instead of retrying the same text.
- Run the relevant tests with run before finishing and make them pass. If the project needs dependencies, install them with run (for example: pip install -e . pytest).
- Every run and edit creates a numbered checkpoint. If something goes wrong, use restore to go back to an earlier checkpoint.
- For work with several steps, write a todo list with todo_write first and update it as you go.
- Read a file before editing it, and read it again after commands that may have changed it.
- Put reproduction and debugging scripts under /tmp/praxbot/scratch (write_file accepts that path), never in the repository. Only files that belong to the fix should be left in the repository.
- Never invent results. Report only what the tools returned.
- When the task is complete and verified, call finish with a short summary of what changed and how it was verified. If you cannot complete the task, call finish and say so honestly.

Respond only with tool calls."""

NUDGE = "Continue by calling a tool. Call finish when the task is complete or if you are stuck."
REPEAT_NUDGE = (
    "You have made the same call with the same arguments three times; its result will not change. "
    "Use the results you already have, try a different approach, or call finish and explain what is blocking you."
)
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
    ) -> None:
        self.llm = llm
        self.toolbox = toolbox
        self.investigate_model = investigate_model
        self.investigate_steps = investigate_steps
        self.max_steps = max_steps
        self.max_transcript_chars = max_transcript_chars
        self.on_event = on_event or _noop
        self.tools = tool_specs()

    def run(self, task: str) -> AgentResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        instructions = getattr(self.toolbox, "project_instructions", None)
        if instructions is not None:
            try:
                text = instructions()
            except Exception:  # noqa: BLE001
                text = ""
            if text:
                messages.append({"role": "user", "content": text})
                self.on_event("project.instructions", chars=len(text))
        repo_map = getattr(self.toolbox, "repo_map", None)
        if repo_map is not None:
            try:
                overview = repo_map(task)
            except Exception as e:  # noqa: BLE001 - the map is a convenience, never a blocker
                overview = ""
                self.on_event("repo.map", chars=0, error=f"{type(e).__name__}: {str(e)[:120]}")
            if overview:
                messages.append({
                    "role": "user",
                    "content": "Repository map, definitions with line numbers. Use it to decide what to read; "
                    "read regions with start_line and end_line, and call outline for files not shown.\n" + overview,
                })
                self.on_event("repo.map", chars=len(overview))
        call_names: dict[str, str] = {}
        compact = getattr(self.toolbox, "compact", None)
        nudges = 0
        recent_calls: list[str] = []
        step = 0  # model turns that called at least one tool; turns without a tool call do not count
        edited = False
        empty_finish_refused = False
        while True:
            force_finish = step >= self.max_steps - 1 or nudges >= 2
            investigating = self.investigate_model and step < self.investigate_steps and not edited
            model = self.investigate_model if investigating else None
            compacted = compact_old_tool_results(
                messages, max_chars=self.max_transcript_chars, names=call_names, compact=compact
            )
            if compacted:
                self.on_event("context.elided", count=compacted)

            msg = self.llm.complete(messages, self.tools, tool_choice=FORCE_FINISH if force_finish else "auto", model=model)
            messages.append(assistant_message(msg))
            self.on_event("llm.call", step=step + 1, usage=self.llm.usage, model=model or getattr(self.llm, "model", ""))

            calls = getattr(msg, "tool_calls", None) or []
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
                    if not edited and not empty_finish_refused and not force_finish:
                        empty_finish_refused = True
                        output = ("error: no changes have been made to the repository. If the task cannot be done, call finish "
                                  "again and explain why in the summary; otherwise keep working.")
                        self.on_event("tool.result", step=step, name=name, output=output)
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})
                        call_names[tc.id] = name
                        continue
                    summary = _finish_summary(raw_args)
                    self.on_event("finish", step=step, summary=summary)
                    return AgentResult(True, summary, step, "finish")
                output = self.toolbox.call(name, raw_args)
                if name in ("edit_file", "write_file"):
                    edited = True
                if name == "todo_write":
                    self.on_event("todo", step=step, todos=list(getattr(self.toolbox, "todos", [])))
                output = f"{output}\n(step {step} of {self.max_steps})"
                self.on_event("tool.result", step=step, name=name, output=output)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})
                call_names[tc.id] = name
                recent_calls = (recent_calls + [call_signature(name, raw_args)])[-REPEAT_WINDOW:]

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
    except Exception:  # noqa: BLE001 - invalid arguments are their own kind of repeat
        return f"{name}:{raw_args}"


def _finish_summary(raw_args: str) -> str:
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        return raw_args.strip() or "(no summary given)"
    if isinstance(args, dict) and args.get("summary"):
        return str(args["summary"])
    return "(no summary given)"
