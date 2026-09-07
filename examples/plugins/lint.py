"""Example Prax Bot plugin: a `lint` tool that runs ruff inside the sandbox.

Install it with: cp examples/plugins/lint.py ~/.praxbot/plugins/
A plugin defines register(add) and calls add(name, description, ArgsModel, handler) per tool.
The handler receives the workspace and the parsed arguments and returns the text the model sees.
It runs on your machine; anything it wants to run in the sandbox goes through workspace.run.
"""

from __future__ import annotations

import shlex

from pydantic import BaseModel, Field


class LintArgs(BaseModel):
    path: str = Field(".", description="File or directory to lint, relative to the repository root")


def lint(workspace, args: LintArgs) -> str:
    target = shlex.quote(args.path.strip() or ".")
    command = f"(ruff --version >/dev/null 2>&1 || pip install -q ruff) && ruff check --output-format concise {target}"
    r = workspace.run(command, timeout=300, mutate=False)
    out = (r.stdout or "").strip() or (r.stderr or "").strip()
    verdict = "clean" if r.exit_code == 0 else f"exit {r.exit_code}"
    return f"ruff check {args.path}: {verdict}\n{out[-6000:]}"


def register(add) -> None:
    add("lint", "Run ruff on a file or directory and report the findings. Cheaper than running the tests for style and unused-import problems.", LintArgs, lint)
