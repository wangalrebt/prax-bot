"""Scripted model and workspace fakes shared by behavior tests."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

from contree_sdk.sdk.exceptions import NotFoundError

from prax_bot.core.llm import Usage
from prax_bot.workspace.repository import resolve_in_workdir
from prax_bot.workspace.types import Checkpoint, CommandResult


def call(name: str, **args: object) -> SimpleNamespace:
    return SimpleNamespace(id=f"call-{name}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def reply(content: str | None = None, *calls: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(content=content, tool_calls=list(calls))


class FakeLLM:
    def __init__(self, replies: list[SimpleNamespace]) -> None:
        self.replies = list(replies)
        self.requests: list[tuple[list[dict], object]] = []
        self.usage = Usage()

    def complete(self, messages, tools, *, tool_choice="auto", temperature=0.2, model=None):
        self.requests.append(([dict(m) for m in messages], tool_choice))
        self.usage.calls += 1
        if not self.replies:
            return reply("nothing left to say")
        return self.replies.pop(0)


class FakeToolBox:
    def __init__(self) -> None:
        self.ws = FakeWorkspace()
        self.calls: list[tuple[str, str]] = []

    def call(self, name: str, raw_args: str):
        from prax_bot.core.tool_result import ToolResult

        self.calls.append((name, raw_args))
        return ToolResult(f"ok {name}")


class FakeWorkspace:
    """In-memory stand-in for the sandbox workspace: files only, no shell."""

    backend = "contree"
    workdir = "/work"

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = {k: v.encode() for k, v in (files or {}).items()}
        self.checkpoints = [Checkpoint(0, "img-0", "baseline")]

    @property
    def current_step(self) -> int:
        return self.checkpoints[-1].step

    def read(self, path: str) -> bytes:
        resolve_in_workdir(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write(self, path: str, content: bytes) -> None:
        resolve_in_workdir(path)
        self.files[path] = content
        self.checkpoints.append(Checkpoint(len(self.checkpoints), f"img-{len(self.checkpoints)}", f"write: {path}"))

    def restore(self, step: int) -> Checkpoint:
        target = self.checkpoints[step]
        self.checkpoints.append(Checkpoint(len(self.checkpoints), target.image, f"restore: step {step}"))
        return target


class ShellFakeWorkspace(FakeWorkspace):
    """FakeWorkspace plus a scripted `run`, so list/search error paths can be exercised."""

    def __init__(self, files=None, *, results=None):
        super().__init__(files)
        self.results = list(results or [])
        self.commands: list[str] = []

    def run(self, command, *, timeout=None, mutate=True, truncate_output_at=None):
        self.commands.append(command)
        exit_code, stdout, stderr, truncated = self.results.pop(0)
        return CommandResult(command, exit_code, stdout, stderr, 0.01, truncated, False, None, 0.0)


class FakeImage:
    """Mimics the ConTree SDK image object: run() prepares, wait() executes and yields a new image."""

    counter = 0
    registry: dict[str, dict[str, bytes]] = {}

    def __init__(self, uuid: str | None, fs: dict[str, bytes], log: list) -> None:
        self.uuid = uuid
        self.tag = None
        self.fs = dict(fs)
        self.log = log
        self._pending: dict | None = None
        self.exit_code = 0
        self.stdout = ""
        self.stderr = ""
        self.fail_bootstrap = False

    def run(self, *, shell, cwd=None, timeout=None, disposable=True, truncate_output_at=None, files=None):
        child = FakeImage(None, self.fs, self.log)
        child.fail_bootstrap = self.fail_bootstrap
        child._pending = {
            "shell": shell,
            "cwd": cwd,
            "timeout": timeout,
            "disposable": disposable,
            "files": dict(files or {}),
            "truncate_output_at": truncate_output_at,
        }
        return child

    def wait(self):
        p = self._pending
        assert p is not None
        self.log.append(p)
        for path, content in p["files"].items():
            self.fs[path] = content
        self.stdout, self.exit_code = self._exec(p["shell"])
        FakeImage.counter += 1
        self.uuid = None if p["disposable"] else f"img-{FakeImage.counter}"
        if self.uuid:
            FakeImage.registry[self.uuid] = dict(self.fs)
        self.result = SimpleNamespace(
            cost=0.001,
            elapsed_time=timedelta(seconds=0.5),
            truncated=False,
            _raw=SimpleNamespace(result=SimpleNamespace(state=SimpleNamespace(timed_out=False))),
        )
        return self

    def _exec(self, shell: str) -> tuple[str, int]:
        if shell.startswith("mkdir -p /work && tar -xf"):
            return ("", 1) if self.fail_bootstrap else ("", 0)
        if shell.startswith("mkdir -p") and " && mv -f /tmp/praxbot.upload " in shell:
            target = shell.split(" && mv -f /tmp/praxbot.upload ")[1].strip().strip("'")
            self.fs[target] = self.fs.pop("/tmp/praxbot.upload")
            return "", 0
        if shell.startswith("git add -A && git rev-parse HEAD"):
            return "0" * 40 + "\nM\0x\0", 0  # name-status: one modified file
        if shell.startswith("git ls-files --others --ignored"):
            return "", 0
        if shell.startswith("git add -A && git --literal-pathspecs diff"):
            return "diff --git a/x b/x\n", 0
        if shell == "touch marker":
            self.fs["/work/marker"] = b""
            return "", 0
        if shell == "fail":
            return "", 3
        return "ok\n", 0

    def read(self, path: str) -> bytes:
        if path not in self.fs:
            raise NotFoundError.__new__(NotFoundError)
        return self.fs[path]


def fake_client(log: list):
    def use(ref: str):
        return FakeImage(ref, FakeImage.registry[ref], log)

    return SimpleNamespace(images=SimpleNamespace(use=use, oci=lambda ref, timeout=None: base(log)))


def base(log: list) -> FakeImage:
    img = FakeImage("base", {"/work/a.py": b"print(1)\n"}, log)
    FakeImage.registry["base"] = dict(img.fs)
    return img


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1),
        )
