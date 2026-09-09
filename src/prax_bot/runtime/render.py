"""Terminal rendering of run events with Rich."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.markup import escape

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
            style = "red" if output.startswith("error:") else "dim"
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
