"""Tools the model can call. Every one of them acts on the sandbox workspace."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shlex
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from contree_sdk.sdk.exceptions import ContreeError
from pydantic import BaseModel, Field, ValidationError

from prax_bot.core.context import ELIDED_MARK, compact_tool_output
from prax_bot.core.editing import apply_edit
from prax_bot.core.navigation import (
    DEFINITION_RE,
    definition_span,
    enclosing_definition,
    number_lines,
    rank_files,
    references_in_output,
)
from prax_bot.core.prompts import prompt
from prax_bot.core.tool_result import ToolResult
from prax_bot.core.verification import pytest_failures
from prax_bot.workspace.repository import SCRATCH_DIR, resolve_in_workdir
from prax_bot.workspace.types import Workspace, WorkspaceError

SEARCH_EXCLUDED_DIRS = (".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache")
REPO_MAP_BUDGET = 8_000
MAX_RESTORES = 3
OUTPUT_DIR = "${TMPDIR:-/tmp}/praxbot"
OUTPUT_TAIL_BYTES = 20_000
REPO_MAP_MAX_FILES = 60

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
    symbol: str = Field(
        "",
        description="A function or class to read whole, by the name outline lists; Class.method for a method. "
        "Overrides start_line and end_line.",
    )


class EditFile(BaseModel):
    path: str = Field(..., description="Existing file relative to the repository root")
    old: str = Field(
        ..., description="Exact text to replace. It must occur exactly once in the file unless replace_all is true."
    )
    new: str = Field(..., description="Replacement text")
    replace_all: bool = Field(False, description="Replace every exact occurrence instead of requiring a unique one")


class WriteFile(BaseModel):
    path: str = Field(..., description="New file relative to the repository root")
    content: str


class Run(BaseModel):
    command: str = Field(..., description="Shell command, run in the repository root (or workdir)")
    timeout: int = Field(0, ge=0, le=3600, description="Seconds; 0 means the default of 300")
    workdir: str = Field(
        ".",
        description="Directory to run in, relative to the repository root. State such as cwd does not persist between calls, so use this instead of cd.",
    )


class TodoItem(BaseModel):
    content: str = Field(..., description="A short imperative line")
    status: str = Field(
        ..., pattern="^(pending|in_progress|completed)$", description="pending, in_progress, or completed"
    )


class TodoWrite(BaseModel):
    todos: list[TodoItem] = Field(..., description="The complete list; it replaces the previous list")


class Restore(BaseModel):
    step: int = Field(..., ge=0, description="Checkpoint number to restore the workspace to")


class LoadSkill(BaseModel):
    name: str = Field(..., description="The skill's name, from the list of available skills")


class Finish(BaseModel):
    summary: str = Field(
        ..., description="Outcome, changes, observed check results, pending checks, and remaining work"
    )


class GoalFinish(Finish):
    goal_met: bool = Field(
        False,
        description="True when available evidence supports the whole goal; report pending runtime checks in summary",
    )


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

TOOL_DESCRIPTIONS = {name: prompt(f"tools/{name}", scratch_dir=SCRATCH_DIR) for name in TOOL_MODELS}
LOAD_SKILL_DESCRIPTION = prompt("tools/load_skill")
PROTECTED_MSG = prompt("protected", scratch_dir=SCRATCH_DIR)


def tool_specs(*, goal: bool = False) -> list[dict[str, Any]]:
    """The tool schemas sent to the model. In goal runs, finish also takes goal_met."""
    specs = []
    models = {**TOOL_MODELS, "finish": GoalFinish} if goal else TOOL_MODELS
    for name, model in models.items():
        schema = model.model_json_schema()
        schema.pop("title", None)
        specs.append(
            {
                "type": "function",
                "function": {"name": name, "description": TOOL_DESCRIPTIONS[name], "parameters": schema},
            }
        )
    return specs


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


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def failed(message: str) -> ToolResult:
    """A tool result the model reads as a failure and the runtime marks as one."""
    return ToolResult(f"error: {message}", error=True)


def clipped(text: str, limit: int) -> str:
    return text[:limit] + ("\n… (truncated)" if len(text) > limit else "")


class ToolBox:
    def __init__(
        self,
        workspace: Workspace,
        *,
        protected: Sequence[str] = (),
        skills: Sequence[Any] = (),
        plugins: Sequence[Any] = (),
        known_failures: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self.ws = workspace
        self._known_failures = known_failures  # tests failing on the clean baseline, once a parallel fork has run them
        self.protected = tuple(protected)
        self._paths: list[str] = []
        self._defs: dict[str, list[tuple[int, str]]] = {}
        self._restores = 0
        self._edit_steps: list[int] = []  # checkpoints at which a repository file was changed
        self._skills: dict[str, Any] = {sk.name: sk for sk in skills}
        self._extra: dict[str, Any] = {t.name: t for t in plugins}
        self._shown_instructions: set[str] = set()  # nested AGENTS.md files already shown to the model
        self._reported_failures: dict[tuple[str, str], list[str]] = {}
        self._baseline_failures: set[str] = (
            set()
        )  # tests failing without the change: seen before any edit, or by the fork
        self._told_baseline: set[str] = set()  # the ones the model has already been told about
        self._observed: dict[str, str] = {}  # path -> digest of the content the model last saw
        self._outputs = 0
        self.todos: list[dict[str, str]] = []

    def definitions(self, paths: list[str]) -> dict[str, list[tuple[int, str]]]:
        """Line-numbered definitions per file, from one grep over all files not yet cached."""
        wanted = [p for p in paths if p not in self._defs]
        if wanted:
            for p in wanted:
                self._defs[p] = []
            quoted = " ".join(shlex.quote(p) for p in wanted)
            r = self.ws.run(
                f"grep -nHE {shlex.quote(DEFINITION_RE)} -- {quoted}",
                timeout=60,
                mutate=False,
                truncate_output_at=2 * 1024 * 1024,
            )
            for line in r.stdout.splitlines():
                m = re.match(r"(.+?):(\d+):(.*)$", line)
                if m and m.group(1) in self._defs:
                    self._defs[m.group(1)].append((int(m.group(2)), m.group(3).rstrip()))
        return {p: self._defs.get(p, []) for p in paths}

    def defs_for(self, path: str) -> list[tuple[int, str]]:
        return self._defs.get(path, [])

    def compact(self, name: str, output: str) -> str:
        return compact_tool_output(name, output, self.defs_for)

    def specs(self, *, goal: bool = False) -> list[dict[str, Any]]:
        """Tool schemas for this run: the built-ins, load_skill when skills exist, and plugin tools."""
        from prax_bot.extensions.plugins import ExtraTool

        specs = tool_specs(goal=goal)
        if self._skills and "load_skill" not in self._extra:
            self._extra["load_skill"] = ExtraTool("load_skill", LOAD_SKILL_DESCRIPTION, LoadSkill, self._load_skill)
        return specs + [t.spec() for t in self._extra.values()]

    def skills_overview(self) -> str:
        """Names and descriptions of the skills; the repository's own are discovered from its file list."""
        from prax_bot.extensions.skills import overview, repo_skills

        if not self._paths and getattr(self.ws, "run", None):
            self._list_paths()
        for sk in repo_skills(self.ws, self._paths):
            self._skills.setdefault(sk.name, sk)
        return overview(list(self._skills.values()))

    def _load_skill(self, ws: Any, a: LoadSkill) -> str | ToolResult:
        sk = self._skills.get(a.name.strip())
        if sk is None:
            return failed(f"no skill named {a.name!r}. Available: {', '.join(self._skills) or 'none'}")
        return prompt("skill_loaded", name=sk.name, source=sk.source, text=sk.text())

    def nested_instructions(self, path: str, limit: int = 3_000) -> str:
        """The nearest AGENTS.md above a path, below the repository root, the first time that directory is touched."""
        rel = resolve_in_workdir(path, self.ws.workdir)[len(self.ws.workdir) :].strip("/")
        parts = rel.split("/")[:-1]
        for depth in range(len(parts), 0, -1):
            directory = "/".join(parts[:depth])
            for name in ("AGENTS.md", "CLAUDE.md"):
                candidate = f"{directory}/{name}"
                if candidate in self._shown_instructions:
                    return ""
                if self._paths and candidate not in self._paths:
                    continue
                try:
                    text = self.ws.read(candidate).decode("utf-8", "replace").strip()
                except (FileNotFoundError, IsADirectoryError, ValueError, OSError):
                    continue
                self._shown_instructions.add(candidate)
                if not text:
                    return ""
                return "\n\n" + prompt("nested_instructions", path=candidate, text=clipped(text, limit))
        return ""

    def project_instructions(self, limit: int = 4_000) -> str:
        """Contents of the repository's agent instruction file, if it has one."""
        for name in ("AGENTS.md", "CLAUDE.md", ".praxbot.md"):
            try:
                text = self.ws.read(name).decode("utf-8", "replace").strip()
            except (FileNotFoundError, IsADirectoryError, ValueError):
                continue
            if text:
                return prompt("project_instructions", name=name, text=clipped(text, limit))
        return ""

    def _list_paths(self) -> list[str]:
        """Every path git would track or does not ignore, kept for the run so later lookups need no command."""
        r = self.ws.run(
            "git ls-files -co --exclude-standard", timeout=60, mutate=False, truncate_output_at=2 * 1024 * 1024
        )
        self._paths = r.stdout.splitlines()
        return self._paths

    def repo_map(self, task: str, budget: int = REPO_MAP_BUDGET) -> str:
        """Definitions with line numbers for the most relevant source files, within a character budget."""
        files = rank_files(self._list_paths(), task)[:REPO_MAP_MAX_FILES]
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

    def call(self, name: str, raw_args: str | dict[str, Any] | None) -> ToolResult:
        extra = self._extra.get(name)
        model = extra.model if extra else TOOL_MODELS.get(name)
        if model is None or name == "finish":
            available = list(TOOL_MODELS) + list(self._extra)
            return failed(f"unknown tool {name!r}. Available tools: {', '.join(available)}")
        if ELIDED_MARK in (raw_args if isinstance(raw_args, str) else json.dumps(raw_args or {})):
            return failed(
                "the arguments contain a placeholder the runtime puts into old calls to save context, not real text. "
                "Read the file for its current content and send the actual text."
            )
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            if not isinstance(args, dict):
                raise TypeError("arguments must be a JSON object")
            parsed = model.model_validate(args)
        except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as e:
            return failed(f"invalid arguments for {name}: {e}")
        try:
            returned = extra.handler(self.ws, parsed) if extra else getattr(self, f"_{name}")(parsed)
            result = returned if isinstance(returned, ToolResult) else ToolResult(returned)
            path = getattr(parsed, "path", None)
            if path and name in ("read_file", "edit_file", "write_file", "outline") and not result.error:
                result = ToolResult(result.output + self.nested_instructions(path))
            return result
        except FileNotFoundError as e:
            return failed(f"file not found: {e}")
        except IsADirectoryError as e:
            return failed(f"{e} is a directory, not a file")
        except (ValueError, WorkspaceError) as e:
            return failed(str(e))
        except (ContreeError, OSError) as e:
            return failed(
                f"the workspace backend failed ({type(e).__name__}: {str(e)[:300]}). "
                "Try the call again; if it keeps failing, call finish and say so."
            )

    def _list_files(self, a: ListFiles) -> str | ToolResult:
        path = a.path.strip() or "."
        resolve_in_workdir(path, self.ws.workdir)
        r = self.ws.run(f"git ls-files -co --exclude-standard -- {shlex.quote(path)}", timeout=60, mutate=False)
        if r.exit_code != 0:
            return failed(r.stderr.strip() or "listing failed")
        lines = r.stdout.splitlines()
        if not lines:
            return f"(no files under {path})"
        return _limited(lines, a.max_entries, r.truncated, "entries; narrow the path or raise max_entries")

    def _search(self, a: Search) -> str | ToolResult:
        path = a.path.strip() or "."
        resolve_in_workdir(path, self.ws.workdir)
        excludes = " ".join(f"--exclude-dir={d}" for d in SEARCH_EXCLUDED_DIRS)
        flags = "-rnI" + ("i" if a.ignore_case else "")
        note = ""
        r = self.ws.run(
            f"grep {flags} -E {excludes} -e {shlex.quote(a.pattern)} -- {shlex.quote(path)}", timeout=60, mutate=False
        )
        if r.exit_code > 1 and not r.stdout.strip():
            # not a valid extended regex (unbalanced parentheses and the like): search for it literally
            r = self.ws.run(
                f"grep {flags} -F {excludes} -e {shlex.quote(a.pattern)} -- {shlex.quote(path)}",
                timeout=60,
                mutate=False,
            )
            note = "\n(the pattern was not a valid regular expression, so it was searched for literally)"
        lines = r.stdout.splitlines()
        if not lines:
            if r.exit_code > 1:
                hint = ""
                if "No such file" in r.stderr and self._paths:
                    tops = sorted({p.split("/", 1)[0] for p in self._paths if "/" in p})
                    hint = f" The repository's top-level directories are: {', '.join(tops[:25])}."
                return failed(f"{r.stderr.strip() or 'search failed'}.{hint}")
            return (
                "no matches. Do not guess names: search for a shorter substring, set ignore_case, "
                "or call outline on the file to see what it defines." + note
            )
        return clip(_limited(lines, a.max_results, r.truncated, "matches; narrow the pattern or path")) + note

    def _observe(self, path: str, text: str) -> None:
        self._observed[path] = _digest(text)

    def _fresh(self, path: str, text: str) -> bool:
        """The model has seen exactly this content, from a read or from its own last edit."""
        return self._observed.get(path) == _digest(text)

    def _protected(self, path: str) -> bool:
        """True for an existing repository file matching a protected glob. New files and the scratch dir are free."""
        raw = path.strip()
        if raw.startswith(SCRATCH_DIR):
            return False
        rel = raw.lstrip("./")
        if not any(fnmatch.fnmatch(rel, pat) for pat in self.protected):
            return False
        try:
            self.ws.read(path)
        except (FileNotFoundError, IsADirectoryError, ValueError, WorkspaceError):
            return False
        return True

    def _read_file(self, a: ReadFile) -> str | ToolResult:
        text = self.ws.read(a.path).decode("utf-8", "replace")
        self._observe(a.path, text)
        lines = text.splitlines()
        total = len(lines)
        if total == 0:
            return f"{a.path} is empty"
        start, end, label = a.start_line, a.end_line or total, ""
        if a.symbol:
            span = definition_span(text, a.symbol)
            if not isinstance(span, tuple):
                hint = f"; did you mean {', '.join(span)}" if span else "; call outline to see what it defines"
                return failed(f"no definition named {a.symbol!r} in {a.path}{hint}")
            start, end = span
            label = f" ({a.symbol})"
        if start > total:
            return failed(f"{a.path} has {total} lines; start_line {start} is past the end")
        end = min(end, total, start + MAX_READ_LINES - 1)
        body = number_lines(lines[start - 1 : end], start)
        note = "" if end == total else f"\n... {total - end} more lines; call read_file with start_line={end + 1}"
        return f"{a.path} lines {start}-{end} of {total}{label}\n{body}{note}"

    def _outline(self, a: Outline) -> str:
        self.ws.read(a.path)  # existence and path checks
        entries = self.definitions([a.path])[a.path]
        if not entries:
            return f"{a.path} defines no functions or classes that outline recognises; read it directly"
        lines = [f"{ln}:{text}" for ln, text in entries]
        return f"{a.path}: {len(lines)} definitions\n" + _limited(lines, 300, False, "definitions")

    def _edit_file(self, a: EditFile) -> str | ToolResult:
        if self._protected(a.path):
            return failed(f"{a.path} {PROTECTED_MSG}")
        text = self.ws.read(a.path).decode("utf-8", "replace")
        if not self._fresh(a.path, text):
            why = (
                "It has changed since you last read it."
                if a.path in self._observed
                else "You have not read it in this session."
            )
            return failed(f"read {a.path} before editing it. {why}")
        new_text, error = apply_edit(text, a.old, a.new, replace_all=a.replace_all)
        if error:
            return failed(error)
        self.ws.write(a.path, new_text.encode("utf-8"))
        self._after_write(a.path, new_text)
        return (
            f"edited {a.path}: replaced {a.old.count(chr(10)) + 1} lines with {a.new.count(chr(10)) + 1} lines"
            f" (checkpoint {self.ws.current_step})"
        )

    def _write_file(self, a: WriteFile) -> str | ToolResult:
        if self._protected(a.path):
            return failed(f"{a.path} {PROTECTED_MSG}")
        try:
            self.ws.read(a.path)
        except FileNotFoundError:
            pass
        else:
            return failed(f"{a.path} already exists; use edit_file to change it")
        self.ws.write(a.path, a.content.encode("utf-8"))
        self._after_write(a.path, a.content)
        output = f"wrote {a.path} ({len(a.content)} characters, checkpoint {self.ws.current_step})"
        path = resolve_in_workdir(a.path, self.ws.workdir)
        if self.ws.backend == "local" and path.startswith(SCRATCH_DIR + "/"):
            shell_path = '"$TMPDIR/praxbot/scratch"/' + shlex.quote(path.removeprefix(SCRATCH_DIR + "/"))
            output += "\n" + prompt("local_scratch_written", shell_path=shell_path, file_path=path)
        return output

    def _run(self, a: Run) -> str | ToolResult:
        workdir = a.workdir.strip() or "."
        resolve_in_workdir(workdir, self.ws.workdir)
        self._outputs += 1
        log = f"{OUTPUT_DIR}/out-{self._outputs}.log"
        # Run in the requested directory; keep the complete output in a file and return its tail.
        inner = f"cd {shlex.quote(workdir)} && {{ {a.command}\n}}"
        wrapped = (
            # a subshell, so an `exit` in the command ends only the command and its output is still returned
            f"mkdir -p {OUTPUT_DIR}; ( {inner} ) > {log} 2>&1; rc=$?; "
            f'tail -c {OUTPUT_TAIL_BYTES} {log}; echo; echo "__praxbot_bytes=$(wc -c < {log})"; exit $rc'
        )
        r = self.ws.run(wrapped, timeout=a.timeout or None, mutate=True)
        self._defs.clear()  # the command may have changed any file
        output, size = r.stdout, None
        m = re.search(r"__praxbot_bytes=\s*(\d+)\s*$", output)  # BSD wc, on macOS, pads the count with spaces
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
            parts.append(
                f"(showing the last {OUTPUT_TAIL_BYTES:,} of {size:,} bytes; the full output is in {log}, grep or sed it with run)"
            )
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
        note = self._note_test_observations(output + "\n" + r.stderr, command=a.command, workdir=workdir)
        if note:
            parts.append(note)
        return ToolResult("\n".join(parts), error=r.exit_code != 0 or r.timed_out)

    def _todo_write(self, a: TodoWrite) -> str:
        self.todos = [t.model_dump() for t in a.todos]
        counts = {s: sum(1 for t in self.todos if t["status"] == s) for s in ("pending", "in_progress", "completed")}
        marks = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        lines = [f"{marks[t['status']]} {t['content']}" for t in self.todos]
        return (
            f"todo list recorded: {counts['completed']} done, {counts['in_progress']} in progress, {counts['pending']} pending\n"
            + "\n".join(lines)
        )

    def _note_test_observations(self, output: str, *, command: str, workdir: str) -> str:
        """What this test output means next to earlier ones, without inferring passes that were not reported.

        Failures seen before any repository edit are the baseline: the model is told about them once, and a
        later run that still shows them is told they are not its doing. Failures repeated by the same command
        after an edit are pointed out, and a test explicitly reported passing after failing earlier is named.
        """
        failures = pytest_failures(output)
        if self._known_failures is not None:
            self._baseline_failures.update(self._known_failures())
        scope = (command, workdir)
        previous = self._reported_failures.get(scope, [])
        passed = set(re.findall(r"^(?:PASSED (\S+)|(\S+) PASSED(?:\s|$))", output, re.MULTILINE))
        passed_ids = {name for pair in passed for name in pair if name}
        self._reported_failures[scope] = list(dict.fromkeys([*previous, *failures]))
        notes = []
        if not self.repo_edited:
            new_baseline = [name for name in failures if name not in self._told_baseline]
            self._baseline_failures.update(failures)
            self._told_baseline.update(failures)
            if new_baseline:
                notes.append(
                    f"{len(new_baseline)} tests fail before any edit; failures in this list later are not caused by "
                    "your changes: " + ", ".join(new_baseline[:12]) + (" …" if len(new_baseline) > 12 else "")
                )
        else:
            old = [name for name in failures if name in self._baseline_failures]
            repeated = [name for name in failures if name in previous and name not in self._baseline_failures]
            if old:
                notes.append(
                    f"{len(old)} of these failures were already failing before your edits: "
                    + ", ".join(old[:8])
                    + (" …" if len(old) > 8 else "")
                )
            if repeated:
                notes.append("also reported failing by this command earlier: " + ", ".join(repeated[:8]))
        confirmed = [name for name in previous if name in passed_ids]
        if confirmed:
            notes.append("explicitly reported passing now: " + ", ".join(confirmed[:8]))
        return "note: " + "; ".join(notes) if notes else ""

    def _after_write(self, path: str, text: str) -> None:
        """After the model changes a file: its outline is stale, it may count as a repository edit, and its content is seen."""
        self._defs.pop(path, None)
        if not resolve_in_workdir(path, self.ws.workdir).startswith(SCRATCH_DIR):
            self._edit_steps.append(self.ws.current_step)
        self._observe(path, text)

    @property
    def repo_edited(self) -> bool:
        """Whether any repository file has been changed and the change is still in the workspace."""
        return bool(self._edit_steps)

    def _restore(self, a: Restore) -> str | ToolResult:
        if self._restores >= MAX_RESTORES:
            return failed(
                f"restore limit of {MAX_RESTORES} reached for this run. Keep the current state; "
                "if tests fail for reasons unrelated to your change, say so in finish."
            )
        if self._edit_steps and all(s > a.step for s in self._edit_steps):
            kept = ", ".join(str(s) for s in self._edit_steps)
            return failed(
                f"restoring to checkpoint {a.step} would discard every change you have made to the repository "
                f"(your edits are checkpoints {kept}). Keep them: restore to a later checkpoint, or undo the one change "
                "that is wrong with edit_file. Unrelated test failures are not a reason to start over."
            )
        self._restores += 1
        target = self.ws.restore(a.step)
        self._edit_steps = [s for s in self._edit_steps if s <= target.step]
        return f"restored the workspace to checkpoint {target.step} ({target.label}); now at checkpoint {self.ws.current_step}"
