"""Tools the model can call. Every one of them acts on the sandbox workspace."""

from __future__ import annotations

import difflib
import json
import re
import shlex
from collections.abc import Sequence
from typing import Any, Callable

from contree_sdk.sdk.exceptions import ContreeError
from pydantic import BaseModel, Field, ValidationError

from .sandbox import SCRATCH_DIR, Workspace, WorkspaceError, resolve_in_workdir

SEARCH_EXCLUDED_DIRS = (".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache")
DEFINITION_RE = r"^\s*(async def|def|class|function|func|fn|struct|impl|trait|interface|type|module)\s+[A-Za-z_]"
SOURCE_SUFFIXES = (".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".rb", ".c", ".h", ".cpp", ".cs", ".kt", ".swift")
TEST_PATH_RE = re.compile(r"(^|/)tests?(/|_)|_test\.py$|(^|/)test_|(^|/)conftest\.py$")
REPO_MAP_BUDGET = 8_000
MAX_RESTORES = 3
OUTPUT_DIR = "${TMPDIR:-/tmp}/praxbot"
OUTPUT_TAIL_BYTES = 20_000
PYTEST_FAILED_RE = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?: - .*)?$", re.M)
REPO_MAP_MAX_FILES = 60
OUTPUT_REF_RE = re.compile(r'File "(?:/work/)?([\w./-]+\.py)", line (\d+)|(?<![\w/])([\w./-]+\.py):(\d+)')

MAX_READ_LINES = 400
MAX_TOOL_OUTPUT = 12_000


class ListFiles(BaseModel):
    path: str = Field(".", description="Directory relative to the repository root")
    max_entries: int = Field(200, ge=1, le=1000)


class Search(BaseModel):
    pattern: str = Field(..., description="Extended regular expression, as for grep -E")
    path: str = Field(".", description="File or directory relative to the repository root")
    max_results: int = Field(50, ge=1, le=200)
    ignore_case: bool = Field(False, description="Case-insensitive search")


class Outline(BaseModel):
    path: str = Field(..., description="File relative to the repository root")


class ReadFile(BaseModel):
    path: str = Field(..., description="File relative to the repository root")
    start_line: int = Field(1, ge=1)
    end_line: int = Field(0, ge=0, description="Last line to read; 0 means to the end (capped at 400 lines per call)")


class EditFile(BaseModel):
    path: str = Field(..., description="Existing file relative to the repository root")
    old: str = Field(..., description="Exact text to replace. It must occur exactly once in the file unless replace_all is true.")
    new: str = Field(..., description="Replacement text")
    replace_all: bool = Field(False, description="Replace every exact occurrence instead of requiring a unique one")


class WriteFile(BaseModel):
    path: str = Field(..., description="New file relative to the repository root")
    content: str


class Run(BaseModel):
    command: str = Field(..., description="Shell command, run in the repository root (or workdir)")
    timeout: int = Field(0, ge=0, le=3600, description="Seconds; 0 means the default of 300")
    workdir: str = Field(".", description="Directory to run in, relative to the repository root. State such as cwd does not persist between calls, so use this instead of cd.")


class TodoItem(BaseModel):
    content: str = Field(..., description="A short imperative line")
    status: str = Field(..., pattern="^(pending|in_progress|completed)$", description="pending, in_progress, or completed")


class TodoWrite(BaseModel):
    todos: list[TodoItem] = Field(..., description="The complete list; it replaces the previous list")


class Restore(BaseModel):
    step: int = Field(..., ge=0, description="Checkpoint number to restore the workspace to")


class Finish(BaseModel):
    summary: str = Field(..., description="What changed, how it was verified, and anything left undone")


TOOL_MODELS: dict[str, type[BaseModel]] = {
    "list_files": ListFiles,
    "search": Search,
    "read_file": ReadFile,
    "outline": Outline,
    "edit_file": EditFile,
    "write_file": WriteFile,
    "run": Run,
    "todo_write": TodoWrite,
    "restore": Restore,
    "finish": Finish,
}

TOOL_DESCRIPTIONS = {
    "list_files": "List files under a directory of the repository (tracked and untracked, ignoring .gitignore'd files).",
    "search": "Search file contents with a regular expression. Returns path:line:text matches.",
    "read_file": "Read a file with line numbers. At most 400 lines per call; use start_line and end_line to read a region.",
    "outline": "List the functions and classes a file defines, with line numbers. Use it to navigate a large file before reading a region.",
    "edit_file": "Replace one exact occurrence of `old` with `new` in an existing file.",
    "write_file": f"Create a new file with the given content. Fails if the file exists; use edit_file instead. Throwaway scripts belong under {SCRATCH_DIR}, not in the repository.",
    "run": "Run a shell command in the repository root inside the sandbox. Returns the exit code and the tail of the output; the full output is saved to a file whose path is reported, and you can grep or sed that file with another run. Each run creates a checkpoint.",
    "todo_write": "Record the plan for multi-step work as a task list and update it as you go. Send the entire list every time; it replaces the previous one.",
    "restore": "Restore the whole workspace to an earlier checkpoint, undoing later edits and commands. Use it only when your own edit broke something that worked before. Tests that were already failing before your edits are not a reason to restore. At most three restores per run.",
    "finish": "Finish the task. Call this once the change is made and verified, or if you cannot complete the task.",
}


def tool_specs() -> list[dict[str, Any]]:
    specs = []
    for name, model in TOOL_MODELS.items():
        schema = model.model_json_schema()
        schema.pop("title", None)
        specs.append(
            {
                "type": "function",
                "function": {"name": name, "description": TOOL_DESCRIPTIONS[name], "parameters": schema},
            }
        )
    return specs


_LINE_PREFIX = re.compile(r"^\s*\d+\|\s?")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _strip_line_numbers(old: str) -> str:
    """If every line carries a read_file-style 'NNN| ' prefix, remove it."""
    lines = old.split("\n")
    if lines and all(_LINE_PREFIX.match(l) for l in lines if l.strip()):
        return "\n".join(_LINE_PREFIX.sub("", l) for l in lines)
    return old


def _reindent(block: list[str], old_lines: list[str], new_lines: list[str]) -> list[str]:
    """Rewrite `new_lines` so unchanged lines keep the file's exact text and changed lines
    are shifted by the indentation difference between the file and what the model sent.

    The model's text is assumed to be consistent with itself but possibly offset from the
    file, and the offset may differ from line to line when it copied sloppily. So each
    changed region borrows the offset of the nearest old line the model did send, which
    is the most reliable local estimate of how far off it was.
    """
    stripped_old = [l.strip() for l in old_lines]
    stripped_new = [l.strip() for l in new_lines]

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
            for k, j in zip(range(i1, i2), range(j1, j2)):
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
    probe = next((l.strip() for l in old.split("\n") if l.strip()), "")
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
        return text, f"old text occurs {count} times; include more surrounding context so it matches exactly once, or set replace_all."

    file_lines = text.split("\n")
    old_lines = old.split("\n")
    if old_lines and old_lines[-1] == "":
        old_lines = old_lines[:-1]
    new_lines = new.split("\n")
    if new_lines and new_lines[-1] == "" and old.endswith("\n"):
        new_lines = new_lines[:-1]
    n = len(old_lines)

    def find(norm):
        target = [norm(l) for l in old_lines]
        return [i for i in range(len(file_lines) - n + 1) if [norm(l) for l in file_lines[i : i + n]] == target]

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


def clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} characters omitted] ...\n{text[-half:]}"


def _limited(lines: list[str], limit: int, truncated: bool, what: str) -> str:
    shown = lines[:limit]
    note = f"\n... {len(lines) - len(shown)} more {what}" if len(lines) > len(shown) else ""
    if truncated:
        note += "\n(the workspace truncated this output)"
    return "\n".join(shown) + note


def number_lines(lines: list[str], start: int) -> str:
    width = len(str(start + len(lines)))
    return "\n".join(f"{start + i:>{width}}| {line}" for i, line in enumerate(lines))


def rank_files(paths: list[str], task: str) -> list[str]:
    """Source files first, files the task mentions before others, tests last unless mentioned."""
    words = {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", task)}

    def score(path: str) -> tuple:
        lower = path.lower()
        stem = lower.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        mentioned = path in task or (stem in words and stem not in {"test", "tests", "init"})
        hits = sum(1 for w in words if len(w) > 3 and w in lower)
        is_test = bool(TEST_PATH_RE.search(path))
        return (not mentioned, is_test and not mentioned, -hits, path.count("/"), path)

    return sorted((p for p in paths if p.endswith(SOURCE_SUFFIXES)), key=score)


def enclosing_definition(defs: list[tuple[int, str]], line: int) -> tuple[int, str] | None:
    best = None
    for start, text in defs:
        if start <= line:
            best = (start, text)
        else:
            break
    return best


def references_in_output(output: str) -> list[tuple[str, int]]:
    """Repository file:line references in a command's output, in order, without duplicates."""
    seen: list[tuple[str, int]] = []
    for m in OUTPUT_REF_RE.finditer(output):
        path = m.group(1) or m.group(3)
        line = int(m.group(2) or m.group(4))
        if path.startswith(("/", "site-packages", "usr/")) or "site-packages" in path or "/.venv/" in path:
            continue
        ref = (path.lstrip("./"), line)
        if ref not in seen:
            seen.append(ref)
    return seen


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
        tail = [l for l in lines[1:] if l.strip()][-3:]
        return f"[compacted] {head}" + (" | " + " | ".join(t.strip()[:100] for t in tail) if tail else "")
    if name == "search":
        refs = [l.split(":", 2)[:2] for l in lines if re.match(r"[^:]+:\d+:", l)]
        shown = ", ".join(":".join(r) for r in refs[:8])
        return f"[compacted] search: {len(refs)} matches" + (f" at {shown}" if shown else f"; {lines[0][:80] if lines else ''}")
    if name == "list_files":
        return f"[compacted] list_files: {len(lines)} entries, first: " + ", ".join(l.strip() for l in lines[:12])
    if name == "outline":
        return "\n".join(lines[:25]) + (f"\n[compacted] … {len(lines) - 25} more definitions" if len(lines) > 25 else "")
    return output if len(output) <= 300 else "[compacted] " + output[:280]


def pytest_failures(output: str) -> list[str]:
    """Test ids reported as FAILED or ERROR in pytest output, in order."""
    return list(dict.fromkeys(m.group(1) for m in PYTEST_FAILED_RE.finditer(output)))


class ToolBox:
    def __init__(self, workspace: Workspace, *, protected: Sequence[str] = ()) -> None:
        self.ws = workspace
        self.protected = tuple(protected)
        self._paths: list[str] = []
        self._defs: dict[str, list[tuple[int, str]]] = {}
        self._restores = 0
        self._first_failures: list[str] | None = None  # failing tests seen in the first test run
        self._edited = False
        self._world = 0  # bumps whenever a command or restore may have changed files
        self._observed: dict[str, int] = {}  # path -> world version at which the model last saw it
        self._outputs = 0
        self.todos: list[dict[str, str]] = []

    def definitions(self, paths: list[str]) -> dict[str, list[tuple[int, str]]]:
        """Line-numbered definitions per file, from one grep over all files not yet cached."""
        wanted = [p for p in paths if p not in self._defs]
        if wanted:
            for p in wanted:
                self._defs[p] = []
            quoted = " ".join(shlex.quote(p) for p in wanted)
            r = self.ws.run(f"grep -nHE {shlex.quote(DEFINITION_RE)} -- {quoted}", timeout=60, mutate=False, truncate_output_at=2 * 1024 * 1024)
            for line in r.stdout.splitlines():
                m = re.match(r"(.+?):(\d+):(.*)$", line)
                if m and m.group(1) in self._defs:
                    self._defs[m.group(1)].append((int(m.group(2)), m.group(3).rstrip()))
        return {p: self._defs.get(p, []) for p in paths}

    def defs_for(self, path: str) -> list[tuple[int, str]]:
        return self._defs.get(path, [])

    def compact(self, name: str, output: str) -> str:
        return compact_tool_output(name, output, self.defs_for)

    def project_instructions(self, limit: int = 4_000) -> str:
        """Contents of the repository's agent instruction file, if it has one."""
        for name in ("AGENTS.md", "CLAUDE.md", ".praxbot.md"):
            try:
                text = self.ws.read(name).decode("utf-8", "replace").strip()
            except (FileNotFoundError, IsADirectoryError, ValueError):
                continue
            if text:
                clipped = text[:limit] + ("\n… (truncated)" if len(text) > limit else "")
                return f"Project instructions from {name}:\n{clipped}"
        return ""

    def repo_map(self, task: str, budget: int = REPO_MAP_BUDGET) -> str:
        """Definitions with line numbers for the most relevant source files, within a character budget."""
        r = self.ws.run("git ls-files -co --exclude-standard", timeout=60, mutate=False, truncate_output_at=2 * 1024 * 1024)
        self._paths = r.stdout.splitlines()
        files = rank_files(self._paths, task)[:REPO_MAP_MAX_FILES]
        if not files:
            return ""
        defs = self.definitions(files)
        parts: list[str] = []
        used = 0
        shown_files = 0
        for path in files:
            entries = defs.get(path) or []
            block = f"{path} ({len(entries)} definitions)"
            if entries:
                block += "\n" + "\n".join(f"  {ln}: {text.strip()[:70]}" for ln, text in entries)
            if used + len(block) > budget:
                if shown_files == 0:
                    block = block[:budget]
                else:
                    break
            parts.append(block)
            used += len(block) + 1
            shown_files += 1
        rest = len(files) - shown_files
        if rest > 0:
            parts.append(f"… {rest} more source files not shown; call outline on any file for its definitions")
        return "\n".join(parts)

    def referenced_definitions(self, output: str) -> str:
        refs = references_in_output(output)[:6]
        if not refs:
            return ""
        defs = self.definitions(sorted({p for p, _ in refs}))
        lines = []
        for path, line in refs:
            if not defs.get(path):
                continue
            enclosing = enclosing_definition(defs[path], line)
            if enclosing:
                lines.append(f"  {path}:{line} is inside {enclosing[1].strip()[:70]} (line {enclosing[0]})")
        return "code referenced above:\n" + "\n".join(lines) if lines else ""

    def call(self, name: str, raw_args: str | dict[str, Any] | None) -> str:
        model = TOOL_MODELS.get(name)
        if model is None or name == "finish":
            return f"error: unknown tool {name!r}. Available tools: {', '.join(TOOL_MODELS)}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
            parsed = model.model_validate(args)
        except (json.JSONDecodeError, ValueError, ValidationError) as e:
            return f"error: invalid arguments for {name}: {e}"
        handler: Callable[[Any], str] = getattr(self, f"_{name}")
        try:
            return handler(parsed)
        except FileNotFoundError as e:
            return f"error: file not found: {e}"
        except IsADirectoryError as e:
            return f"error: {e} is a directory, not a file"
        except (ValueError, WorkspaceError) as e:
            return f"error: {e}"
        except (ContreeError, OSError) as e:
            return (
                f"error: the workspace backend failed ({type(e).__name__}: {str(e)[:300]}). "
                "Try the call again; if it keeps failing, call finish and say so."
            )

    def _list_files(self, a: ListFiles) -> str:
        path = a.path.strip() or "."
        resolve_in_workdir(path, self.ws.workdir)
        r = self.ws.run(f"git ls-files -co --exclude-standard -- {shlex.quote(path)}", timeout=60, mutate=False)
        if r.exit_code != 0:
            return f"error: {r.stderr.strip() or 'listing failed'}"
        lines = r.stdout.splitlines()
        if not lines:
            return f"(no files under {path})"
        return _limited(lines, a.max_entries, r.truncated, "entries; narrow the path or raise max_entries")

    def _search(self, a: Search) -> str:
        path = a.path.strip() or "."
        resolve_in_workdir(path, self.ws.workdir)
        excludes = " ".join(f"--exclude-dir={d}" for d in SEARCH_EXCLUDED_DIRS)
        flags = "-rnI" + ("i" if a.ignore_case else "")
        note = ""
        r = self.ws.run(f"grep {flags} -E {excludes} -e {shlex.quote(a.pattern)} -- {shlex.quote(path)}", timeout=60, mutate=False)
        if r.exit_code > 1 and not r.stdout.strip():
            # not a valid extended regex (unbalanced parentheses and the like): search for it literally
            r = self.ws.run(f"grep {flags} -F {excludes} -e {shlex.quote(a.pattern)} -- {shlex.quote(path)}", timeout=60, mutate=False)
            note = "\n(the pattern was not a valid regular expression, so it was searched for literally)"
        lines = r.stdout.splitlines()
        if not lines:
            if r.exit_code > 1:
                hint = ""
                if "No such file" in r.stderr and self._paths:
                    tops = sorted({p.split("/", 1)[0] for p in self._paths if "/" in p})
                    hint = f" The repository's top-level directories are: {', '.join(tops[:25])}."
                return f"error: {r.stderr.strip() or 'search failed'}.{hint}"
            return (
                "no matches. Do not guess names: search for a shorter substring, set ignore_case, "
                "or call outline on the file to see what it defines." + note
            )
        return clip(_limited(lines, a.max_results, r.truncated, "matches; narrow the pattern or path")) + note

    def _observe(self, path: str) -> None:
        self._observed[path] = self._world

    def _protected(self, path: str) -> bool:
        import fnmatch

        rel = path.strip().lstrip("./")
        return any(fnmatch.fnmatch(rel, pat) for pat in self.protected)

    def _fresh(self, path: str) -> bool:
        return self._observed.get(path) == self._world

    def _read_file(self, a: ReadFile) -> str:
        text = self.ws.read(a.path).decode("utf-8", "replace")
        self._observe(a.path)
        lines = text.splitlines()
        total = len(lines)
        if total == 0:
            return f"{a.path} is empty"
        if a.start_line > total:
            return f"error: {a.path} has {total} lines; start_line {a.start_line} is past the end"
        end = a.end_line or total
        end = min(end, total, a.start_line + MAX_READ_LINES - 1)
        body = number_lines(lines[a.start_line - 1 : end], a.start_line)
        note = "" if end == total else f"\n... {total - end} more lines; call read_file with start_line={end + 1}"
        return f"{a.path} lines {a.start_line}-{end} of {total}\n{body}{note}"

    def _outline(self, a: Outline) -> str:
        self.ws.read(a.path)  # existence and path checks
        entries = self.definitions([a.path])[a.path]
        if not entries:
            return f"{a.path} defines no functions or classes that outline recognises; read it directly"
        lines = [f"{ln}:{text}" for ln, text in entries]
        return f"{a.path}: {len(lines)} definitions\n" + _limited(lines, 300, False, "definitions")

    def _edit_file(self, a: EditFile) -> str:
        if self._protected(a.path):
            return f"error: {a.path} is protected and must not be modified"
        text = self.ws.read(a.path).decode("utf-8", "replace")
        if not self._fresh(a.path):
            return (f"error: read {a.path} before editing it. "
                    + ("It may have changed since you last read it." if a.path in self._observed else "You have not read it in this session."))
        new_text, error = apply_edit(text, a.old, a.new, replace_all=a.replace_all)
        if error:
            return f"error: {error}"
        self.ws.write(a.path, new_text.encode("utf-8"))
        self._defs.pop(a.path, None)
        self._edited = True
        self._observe(a.path)
        return (
            f"edited {a.path}: replaced {a.old.count(chr(10)) + 1} lines with {a.new.count(chr(10)) + 1} lines"
            f" (checkpoint {self.ws.current_step})"
        )

    def _write_file(self, a: WriteFile) -> str:
        if self._protected(a.path):
            return f"error: {a.path} is protected and must not be modified"
        try:
            self.ws.read(a.path)
        except FileNotFoundError:
            pass
        else:
            return f"error: {a.path} already exists; use edit_file to change it"
        self.ws.write(a.path, a.content.encode("utf-8"))
        self._defs.pop(a.path, None)
        self._edited = True
        self._observe(a.path)
        return f"wrote {a.path} ({len(a.content)} characters, checkpoint {self.ws.current_step})"

    def _run(self, a: Run) -> str:
        workdir = a.workdir.strip() or "."
        resolve_in_workdir(workdir, self.ws.workdir)
        self._outputs += 1
        log = f"{OUTPUT_DIR}/out-{self._outputs}.log"
        # Run in the requested directory; keep the complete output in a file and return its tail.
        inner = f"cd {shlex.quote(workdir)} && {{ {a.command}\n}}"
        wrapped = (
            f"mkdir -p {OUTPUT_DIR}; {{ {inner}; }} > {log} 2>&1; rc=$?; "
            f"tail -c {OUTPUT_TAIL_BYTES} {log}; echo; echo \"__praxbot_bytes=$(wc -c < {log})\"; exit $rc"
        )
        r = self.ws.run(wrapped, timeout=a.timeout or None, mutate=True)
        self._defs.clear()  # the command may have changed any file
        self._world += 1
        output, size = r.stdout, None
        m = re.search(r"__praxbot_bytes=(\d+)\s*$", output)
        if m:
            size = int(m.group(1))
            output = output[: m.start()].rstrip("\n")
        status = f"exit code {r.exit_code}"
        if r.timed_out:
            status += " (timed out)"
        header = f"{status} · {r.elapsed_s:.1f}s · checkpoint {self.ws.current_step}"
        parts = [header]
        if output.strip():
            parts.append("output:\n" + clip(output.rstrip(), MAX_TOOL_OUTPUT))
        if r.stderr.strip():
            parts.append("stderr:\n" + clip(r.stderr.rstrip(), MAX_TOOL_OUTPUT // 2))
        if size is not None and size > OUTPUT_TAIL_BYTES:
            parts.append(f"(showing the last {OUTPUT_TAIL_BYTES:,} of {size:,} bytes; the full output is in {log}, grep or sed it with run)")
        elif size is not None:
            parts.append(f"(full output saved in {log})")
        if r.truncated:
            parts.append("(output was truncated by the sandbox)")
        try:
            referenced = self.referenced_definitions(output + "\n" + r.stderr)
        except (WorkspaceError, ValueError):
            referenced = ""
        if referenced:
            parts.append(referenced)
        note = self._note_preexisting_failures(output + "\n" + r.stderr)
        if note:
            parts.append(note)
        return "\n".join(parts)

    def _todo_write(self, a: TodoWrite) -> str:
        self.todos = [t.model_dump() for t in a.todos]
        counts = {s: sum(1 for t in self.todos if t["status"] == s) for s in ("pending", "in_progress", "completed")}
        marks = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        lines = [f"{marks[t['status']]} {t['content']}" for t in self.todos]
        return (f"todo list recorded: {counts['completed']} done, {counts['in_progress']} in progress, {counts['pending']} pending\n"
                + "\n".join(lines))

    def _note_preexisting_failures(self, output: str) -> str:
        """Remember which tests failed before any edit; later, point out failures that are not new."""
        failures = pytest_failures(output)
        if "passed" not in output and "failed" not in output and "error" not in output.lower():
            return ""
        if self._first_failures is None:
            if self._edited:
                return ""
            self._first_failures = failures
            return (f"note: {len(failures)} tests fail before any edit; failures in this list later are not caused by your changes: "
                    + ", ".join(failures[:12]) + (" …" if len(failures) > 12 else "")) if failures else ""
        old = [f for f in failures if f in self._first_failures]
        new = [f for f in failures if f not in self._first_failures]
        fixed = [f for f in self._first_failures if f not in failures]
        bits = []
        if old:
            bits.append(f"{len(old)} of these failures were already failing before your edits: " + ", ".join(old[:8]) + (" …" if len(old) > 8 else ""))
        if new:
            bits.append(f"new failures since the baseline: " + ", ".join(new[:8]))
        if fixed:
            bits.append(f"now passing that failed before: " + ", ".join(fixed[:8]))
        return ("note: " + "; ".join(bits)) if bits else ""

    def _restore(self, a: Restore) -> str:
        if self._restores >= MAX_RESTORES:
            return (f"error: restore limit of {MAX_RESTORES} reached for this run. Keep the current state; "
                    "if tests fail for reasons unrelated to your change, say so in finish.")
        self._restores += 1
        self._world += 1
        target = self.ws.restore(a.step)
        return f"restored the workspace to checkpoint {target.step} ({target.label}); now at checkpoint {self.ws.current_step}"
