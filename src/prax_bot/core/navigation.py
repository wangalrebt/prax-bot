"""Definition lookup and repository navigation helpers."""

from __future__ import annotations

import ast
import re

DEFINITION_RE = r"^\s*(async def|def|class|function|func|fn|struct|impl|trait|interface|type|module)\s+[A-Za-z_]"


SOURCE_SUFFIXES = (
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".c",
    ".h",
    ".cpp",
    ".cs",
    ".kt",
    ".swift",
)


TEST_PATH_RE = re.compile(r"(^|/)tests?(/|_)|_test\.py$|(^|/)test_|(^|/)conftest\.py$")


OUTPUT_REF_RE = re.compile(r'File "(?:/work/)?([\w./-]+\.py)", line (\d+)|(?<![\w/])([\w./-]+\.py):(\d+)')


def definition_span(text: str, symbol: str) -> tuple[int, int] | list[str]:
    """Line range (1-based, inclusive) of the function or class called symbol, decorators included.

    Python files are parsed; other languages use the definition lines outline recognises and end a
    definition where the next one at the same or a shallower indentation starts. When the name is not
    found, or is ambiguous without its class, the candidates are returned instead of a range.
    """
    lines = text.splitlines()
    wanted = symbol.strip().strip("()")
    found: list[tuple[str, int, int]] = []  # (qualified name, start, end)
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        tree = None
    if tree is not None:

        def walk(node: ast.AST, prefix: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = f"{prefix}{child.name}"
                    start = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                    found.append((name, start, child.end_lineno or child.lineno))
                    walk(child, f"{name}.")

        walk(tree, "")
    else:
        starts = [(i + 1, line) for i, line in enumerate(lines) if re.match(DEFINITION_RE, line)]
        for k, (ln, line) in enumerate(starts):
            m = re.match(r"\s*\S+\s+([A-Za-z_][A-Za-z0-9_]*)", line)
            if not m:
                continue
            indent = len(line) - len(line.lstrip())
            end = len(lines)
            for nxt, nxt_line in starts[k + 1 :]:
                if (len(nxt_line) - len(nxt_line.lstrip())) <= indent:
                    end = nxt - 1
                    break
            while end > ln and not lines[end - 1].strip():
                end -= 1
            found.append((m.group(1), ln, end))
    exact = [f for f in found if f[0] == wanted]
    if len(exact) == 1:
        return exact[0][1], exact[0][2]
    by_last = [f for f in found if f[0].rsplit(".", 1)[-1] == wanted.rsplit(".", 1)[-1]]
    if len(by_last) == 1 and not exact:
        return by_last[0][1], by_last[0][2]
    if by_last:
        return [f[0] for f in by_last]
    close = [f[0] for f in found if wanted.lower() in f[0].rsplit(".", 1)[-1].lower()]
    return close[:20]


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
