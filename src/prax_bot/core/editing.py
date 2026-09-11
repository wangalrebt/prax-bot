"""Text replacement with exact and whitespace-tolerant matching."""

from __future__ import annotations

import difflib
import re

_LINE_PREFIX = re.compile(r"^\s*\d+\|\s?")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _strip_line_numbers(old: str) -> str:
    """If every line carries a read_file-style 'NNN| ' prefix, remove it."""
    lines = old.split("\n")
    if lines and all(_LINE_PREFIX.match(line) for line in lines if line.strip()):
        return "\n".join(_LINE_PREFIX.sub("", line) for line in lines)
    return old


def _reindent(block: list[str], old_lines: list[str], new_lines: list[str]) -> list[str]:
    """Rewrite `new_lines` so unchanged lines keep the file's exact text and changed lines
    are shifted by the indentation difference between the file and what the model sent.

    The model's text is assumed to be consistent with itself but possibly offset from the
    file, and the offset may differ from line to line when it copied sloppily. So each
    changed region borrows the offset of the nearest old line the model did send, which
    is the most reliable local estimate of how far off it was.
    """
    stripped_old = [line.strip() for line in old_lines]
    stripped_new = [line.strip() for line in new_lines]

    def offset_at(k: int) -> int:
        """File indent minus model indent for old line k, or the nearest non-blank line before it."""
        for idx in range(min(k, len(old_lines) - 1), -1, -1):
            if old_lines[idx].strip():
                return _indent(block[idx]) - _indent(old_lines[idx])
        for idx in range(len(old_lines)):
            if old_lines[idx].strip():
                return _indent(block[idx]) - _indent(old_lines[idx])
        return 0

    def shifted(line: str, delta: int) -> str:
        return "" if not line.strip() else " " * max(0, _indent(line) + delta) + line.lstrip()

    out: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, stripped_old, stripped_new, autojunk=False).get_opcodes():
        if tag == "equal":
            for k, j in zip(range(i1, i2), range(j1, j2), strict=True):
                # same text; if the model also changed its indentation, it moved the line into or
                # out of a block, and the file's copy must follow
                if _indent(old_lines[k]) == _indent(new_lines[j]):
                    out.append(block[k])
                else:
                    out.append(shifted(new_lines[j], offset_at(k)))
        elif tag in ("replace", "insert"):
            anchor = i1 if tag == "replace" else max(0, i1 - 1)
            delta = offset_at(anchor)
            out.extend(shifted(line, delta) for line in new_lines[j1:j2])
    return out


def closest_region(text: str, old: str, context: int = 2) -> str:
    """Lines of the file around the best match for the first non-empty line of `old`."""
    file_lines = text.split("\n")
    probe = next((line.strip() for line in old.split("\n") if line.strip()), "")
    if not probe:
        return ""
    best, best_i = 0.0, -1
    for i, line in enumerate(file_lines):
        ratio = difflib.SequenceMatcher(None, probe, line.strip(), autojunk=False).ratio()
        if ratio > best:
            best, best_i = ratio, i
    if best_i < 0 or best < 0.5:
        return ""
    lo, hi = max(0, best_i - context), min(len(file_lines), best_i + len(old.split("\n")) + context)
    width = len(str(hi))
    return "\n".join(f"{n + 1:>{width}}| {file_lines[n]}" for n in range(lo, hi))


def apply_edit(text: str, old: str, new: str, *, replace_all: bool = False) -> tuple[str, str | None]:
    """Return (new_text, error). Matching is exact first, then tolerant of trailing
    whitespace, then tolerant of indentation, as long as the match is unique.
    With replace_all, every exact occurrence is replaced and no fuzzy matching is used."""
    if not old:
        return text, "old must not be empty"
    old = _strip_line_numbers(old)

    count = text.count(old)
    if replace_all:
        if count == 0:
            return text, "old text was not found in the file. Read the file and copy the exact text."
        return text.replace(old, new), None
    if count == 1:
        return text.replace(old, new, 1), None
    if count > 1:
        return (
            text,
            f"old text occurs {count} times; include more surrounding context so it matches exactly once, or set replace_all.",
        )

    file_lines = text.split("\n")
    old_lines = old.split("\n")
    if old_lines and old_lines[-1] == "":
        old_lines = old_lines[:-1]
    new_lines = new.split("\n")
    if new_lines and new_lines[-1] == "" and old.endswith("\n"):
        new_lines = new_lines[:-1]
    n = len(old_lines)

    def find(norm):
        target = [norm(line) for line in old_lines]
        return [i for i in range(len(file_lines) - n + 1) if [norm(line) for line in file_lines[i : i + n]] == target]

    for label, norm in (("trailing whitespace", str.rstrip), ("indentation", str.strip)):
        hits = find(norm)
        if len(hits) > 1:
            return text, f"old text matches {len(hits)} places when ignoring {label}; include more surrounding context."
        if len(hits) == 1:
            i = hits[0]
            block = file_lines[i : i + n]
            replacement = new_lines if label == "trailing whitespace" else _reindent(block, old_lines, new_lines)
            merged = file_lines[:i] + replacement + file_lines[i + n :]
            return "\n".join(merged), None

    region = closest_region(text, old)
    hint = "old text was not found in the file, even ignoring indentation."
    if region:
        hint += f" The closest region is:\n{region}\nCopy the exact lines from there."
    else:
        hint += " Read the file and copy the exact text."
    return text, hint
