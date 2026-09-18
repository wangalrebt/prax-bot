"""What a long run carries from call to call: tool results that age out, old call arguments reduced to their size,
and a summarising compaction as the backstop."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from prax_bot.core.prompts import prompt

# How a tool result ages: (results kept whole, ((age limit, characters kept), ...)); past the last tier it is a one-line
# digest. Age counts every tool result since, whatever produced it. File reads are what edits are written from (in
# the benchmark logs 73 of 84 edits followed a read at most five results back), so they stay whole longest. Command
# output, searches and listings are read once and acted on, and they are half of what a long run carries.
READ_POLICY = (6, ((12, 2048), (18, 600)))
OTHER_POLICY = (3, ((6, 1500), (12, 400)))
# The map is for finding the code: it goes once the repository has been edited and this many steps have passed.
REPO_MAP_STEPS = 10
# Tool calls whose arguments stay verbatim. In older calls a file body or an edit text becomes its size, unless it is
# shorter than ARG_STUB_MIN. Tools refuse arguments carrying ELIDED_MARK, so a copied placeholder never reaches a file.
ARGS_KEEP_CALLS = 8
ARG_STUB_MIN = 200
ELIDED_MARK = "[elided by the runtime"
COMPACT_KEEP_TURNS = 3  # assistant turns kept verbatim after a compaction
COMPACT_MIN_GAP = 6  # steps between two compactions
SUMMARY_SYSTEM = prompt("summary")


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

    File reads age by READ_POLICY and every other result by OTHER_POLICY. Runs every step, so the context carries
    recent detail and only a memory of the rest. Returns how many changed.
    """
    tool_indexes = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    changed = 0
    for age, i in enumerate(reversed(tool_indexes)):
        name = (names or {}).get(messages[i].get("tool_call_id", ""), "")
        keep_full, tiers = READ_POLICY if name == "read_file" else OTHER_POLICY
        if age < keep_full:
            continue
        content = messages[i].get("content") or ""
        if content.startswith(("[compacted]", "[elided")):
            continue
        cap = next((size for limit, size in tiers if age < limit), None)
        if cap is None:
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


def stub_old_call_arguments(messages: list[dict[str, Any]], *, keep_last: int = ARGS_KEEP_CALLS) -> int:
    """In tool calls older than the last `keep_last`, replace long string arguments with their size.

    A written file or an edit text is resent with every later call although the workspace already holds it. The
    path and every short argument stay, so the call still says what was done. Returns how many calls changed.
    """
    calls = [tc for m in messages if m.get("role") == "assistant" for tc in m.get("tool_calls") or []]
    changed = 0
    for tc in calls[:-keep_last] if keep_last else calls:
        fn = tc.get("function") if isinstance(tc, dict) else None
        if not isinstance(fn, dict):
            continue
        raw = fn.get("arguments")
        if not isinstance(raw, str) or len(raw) < ARG_STUB_MIN:
            continue
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(args, dict):
            continue
        stubbed = {
            key: f"{ELIDED_MARK} to save context: {len(value):,} characters]"
            if isinstance(value, str) and len(value) >= ARG_STUB_MIN
            else value
            for key, value in args.items()
        }
        if stubbed != args:
            fn["arguments"] = json.dumps(stubbed)
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


def compact_tool_output(name: str, output: str, defs_for: Callable[[str], list[tuple[int, str]]] | None = None) -> str:
    """A short stand-in for an old tool result: enough to remember what it was, not what it said."""
    lines = output.splitlines()
    if name == "read_file":
        header = lines[0] if lines else output[:80]
        m = re.match(r"(\S+) lines (\d+)-(\d+) of (\d+)", header)
        summary = f"[compacted] {header}"
        if m and defs_for is not None:
            lo, hi = int(m.group(2)), int(m.group(3))
            names = [f"{ln}:{text.strip()[:50]}" for ln, text in defs_for(m.group(1)) if lo <= ln <= hi][:12]
            if names:
                summary += "; definitions in that range: " + ", ".join(names)
        return summary + ". Read it again if you need the text."
    if name == "run":
        head = lines[0] if lines else ""
        tail = [line for line in lines[1:] if line.strip()][-3:]
        return f"[compacted] {head}" + (" | " + " | ".join(t.strip()[:100] for t in tail) if tail else "")
    if name == "search":
        refs = [line.split(":", 2)[:2] for line in lines if re.match(r"[^:]+:\d+:", line)]
        shown = ", ".join(":".join(r) for r in refs[:8])
        return f"[compacted] search: {len(refs)} matches" + (
            f" at {shown}" if shown else f"; {lines[0][:80] if lines else ''}"
        )
    if name == "list_files":
        return f"[compacted] list_files: {len(lines)} entries, first: " + ", ".join(line.strip() for line in lines[:12])
    if name == "outline":
        return "\n".join(lines[:25]) + (
            f"\n[compacted] … {len(lines) - 25} more definitions" if len(lines) > 25 else ""
        )
    return output if len(output) <= 300 else "[compacted] " + output[:280]
