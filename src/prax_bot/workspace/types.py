"""The workspace contract implemented by the sandbox and local backends."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

EventFn = Callable[..., None]


def noop(kind: str, **payload: Any) -> None:
    return None


class WorkspaceError(RuntimeError):
    pass


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    elapsed_s: float
    truncated: bool
    timed_out: bool
    image: str | None
    cost: float


@dataclass
class Checkpoint:
    step: int
    image: str
    label: str
    clean: str | None = None  # the clean image at that moment, so a resume can keep verifying in forks of it


@dataclass(frozen=True)
class Changes:
    """A patch and its exact Git baseline, with paths available for review."""

    patch: str
    base: str
    image: str
    files: tuple[str, ...]
    excluded: tuple[str, ...] = ()
    scratch: tuple[
        str, ...
    ] = ()  # new root-level files named like reproduction scripts; in the diff, not applied unless named
    binary: tuple[str, ...] = ()  # new binary files; in the diff, not applied unless named

    @property
    def flagged(self) -> frozenset[str]:
        return frozenset((*self.scratch, *self.binary))


class Workspace(Protocol):
    backend: str
    workdir: str
    command_timeout: int
    on_event: EventFn
    checkpoints: list[Checkpoint]
    total_cost: float
    commands: int
    clean: str | None  # the repository at its baseline, set up, with none of the agent's work; None when unknown

    @property
    def image_id(self) -> str: ...

    @property
    def current_step(self) -> int: ...

    def run(
        self,
        command: str,
        *,
        timeout: int | None = None,
        mutate: bool = True,
        truncate_output_at: int | None = None,
        label: str | None = None,
    ) -> CommandResult: ...

    def read(self, path: str) -> bytes: ...

    def write(self, path: str, content: bytes) -> None: ...

    def restore(self, step: int) -> Checkpoint: ...

    def mark_clean(self) -> None: ...

    def run_clean(self, command: str, *, patch: str = "", timeout: int | None = None) -> CommandResult: ...

    def advance_clean(self, patch: str) -> None: ...

    def changes(self, paths: Sequence[str] | None = None) -> Changes: ...

    def diff(self) -> str: ...
