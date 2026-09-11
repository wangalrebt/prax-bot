"""Terminal rendering of run events with Rich."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax

from prax_bot.settings import Settings

MAX_ARG_PREVIEW = 100
MAX_RESULT_PREVIEW = 160


def _one_line(text: str, limit: int) -> str:
    line = " ".join(text.strip().split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def describe_call(name: str, raw_args: str) -> str:
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        return _one_line(raw_args, MAX_ARG_PREVIEW)
    if not isinstance(args, dict):
        return _one_line(raw_args, MAX_ARG_PREVIEW)
    if name == "run":
        return _one_line(str(args.get("command", "")), MAX_ARG_PREVIEW)
    if name in {"read_file", "write_file", "edit_file"}:
        detail = str(args.get("path", ""))
        if name == "read_file" and args.get("start_line", 1) != 1:
            detail += f" from line {args['start_line']}"
        return detail
    if name == "search":
        return f"{args.get('pattern', '')!r} in {args.get('path', '.')}"
    if name == "list_files":
        return str(args.get("path", "."))
    if name == "restore":
        return f"checkpoint {args.get('step')}"
    return _one_line(json.dumps(args), MAX_ARG_PREVIEW)


class Reporter:
    def __init__(self, console: Console, *, verbose: bool = False) -> None:
        self.console = console
        self.verbose = verbose

    def __call__(self, kind: str, **payload: Any) -> None:
        c = self.console
        if kind == "sandbox.pack":
            c.print(f"  packed repository · {payload['bytes'] / 1e6:.1f} MB")
            for name in payload.get("suspicious") or []:
                c.print(
                    f"  [yellow]warning:[/] {escape(name)} looks like a credential and is being uploaded; add it to .gitignore to keep it out"
                )
        elif kind == "sandbox.base":
            c.print(
                f"  base image {escape(str(payload.get('tag') or ''))} · {payload['image']} · {payload['elapsed']:.1f}s"
            )
        elif kind == "sandbox.ready":
            c.print(f"  [bold]baseline image[/] {payload['image']} · {payload['elapsed']:.1f}s")
        elif kind == "skills":
            if payload.get("error"):
                c.print(f"  [yellow]skills could not be listed:[/] {escape(str(payload['error']))}")
            else:
                c.print(f"  skills · {payload.get('count')} available")
        elif kind == "sandbox.retry":
            c.print(
                f"  [yellow]sandbox {escape(str(payload.get('step')))} failed, retrying (attempt {payload.get('attempt')}):[/] {escape(str(payload.get('error')))}"
            )
        elif kind == "sandbox.setup":
            c.print(f"  setup · exit {payload['exit_code']} · {payload['elapsed']:.1f}s")
        elif kind == "assistant":
            c.print(f"  [dim]{escape(_one_line(payload['text'], 300))}[/]")
        elif kind == "tool.call":
            c.print(
                f"[cyan]▸ {payload['step']:>3}[/]  [bold]{payload['name']}[/]  {escape(describe_call(payload['name'], payload['args']))}"
            )
        elif kind == "tool.result":
            output = payload["output"]
            style = "red" if payload.get("error", False) else "dim"
            if self.verbose:
                c.print(f"       [{style}]{escape(output)}[/]")
            else:
                c.print(
                    f"       [{style}]{escape(_one_line(output.splitlines()[0] if output else '', MAX_RESULT_PREVIEW))}[/]"
                )
        elif kind == "todo":
            done = sum(1 for t in payload["todos"] if t.get("status") == "completed")
            c.print(f"       [dim]todo list: {done}/{len(payload['todos'])} done[/]")
        elif kind == "nudge":
            c.print(f"       [yellow]{escape(payload['reason'])}; asked the model to change approach[/]")
        elif kind == "context.compacted":
            if payload.get("error"):
                c.print(f"       [yellow]context · compaction skipped ({escape(str(payload['error']))})[/]")
            else:
                c.print(
                    f"       [dim]context · {payload['tokens_before']:,} tokens compacted into a summary; the last {payload['kept_turns']} turns kept[/]"
                )
        elif kind == "context.elided":
            c.print(f"       [dim]elided {payload['count']} earlier tool outputs to fit the context[/]")
        elif kind == "finish":
            c.print(f"[green]▸ {payload['step']:>3}[/]  [bold]finish[/]")
        elif kind == "checkpoint" and self.verbose:
            c.print(f"       [dim]checkpoint {payload['step']} · {payload['image']} · {escape(payload['label'])}[/]")

        elif kind == "verification.start":
            c.print(f"[bold]check {escape(payload['name'])}[/] · {escape(payload['command'])}")
        elif kind == "verification.result":
            status = "passed" if payload["passed"] else "failed"
            c.print(f"  {escape(payload['name'])}: {status} · exit {payload['exit_code']}")
            if not payload["passed"]:
                c.print(escape(payload.get("error") or payload.get("output") or "no output"))
        elif kind == "session.round":
            result = payload["result"]
            c.print(
                f"\n{payload['steps_total']} steps · {payload['usage'].total_tokens:,} tokens · "
                f"{payload['checkpoints']} checkpoints · checks {payload['verification']}"
            )
            c.print(
                Panel(
                    escape(result.summary), title="agent finished" if result.finished else f"stopped: {result.reason}"
                )
            )
            print_diff(payload["diff"], Path(payload["run_dir"]), out=c)
        elif kind == "session.applied":
            c.print(f"[green]applied {len(payload['files'])} files to the working tree[/]")
        elif kind == "session.undone":
            c.print(f"restored the workspace and context to checkpoint {payload['checkpoint']}")
        elif kind == "session.resumed":
            c.print(f"resumed {escape(payload['run_id'])} at checkpoint {payload['checkpoint']}")
        elif kind == "session.effort":
            c.print(f"effort {payload['effort']}")
        elif kind == "session.stop_requested":
            c.print("stop requested; waiting for the current model or tool call to finish")


MAX_DIFF_LINES_SHOWN = 400


def sampling_label(settings: Settings) -> str:
    parts = [f"temperature {settings.temperature:g}"]
    if settings.top_p is not None:
        parts.append(f"top_p {settings.top_p:g}")
    parts.append(f"effort {settings.effort}")
    return " · ".join(parts)


def print_diff(diff: str, run_dir: Path, export: Path | None = None, out: Console | None = None) -> None:
    out = out or Console()
    if not diff.strip():
        out.print("no changes to the repository")
        return
    lines = diff.splitlines()
    out.print(Syntax("\n".join(lines[:MAX_DIFF_LINES_SHOWN]), "diff", theme="ansi_dark", word_wrap=False))
    if len(lines) > MAX_DIFF_LINES_SHOWN:
        out.print(f"[dim]… {len(lines) - MAX_DIFF_LINES_SHOWN} more lines in {run_dir / 'diff.patch'}[/]")
    if export:
        export.write_text(diff, encoding="utf-8")
        out.print(f"diff written to {export}")
