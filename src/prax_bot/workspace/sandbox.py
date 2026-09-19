"""The repository inside a Token Factory Sandbox, tracked as a chain of immutable images.

Every mutating command produces a new image and therefore a checkpoint. Read-only
commands run disposably and leave the current image untouched. Because any image can be
run from again, the workspace also keeps a clean image, the repository at its baseline with
setup done and none of the agent's work, and runs checks in disposable forks of it.
"""

from __future__ import annotations

import posixpath
import shlex
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Lock
from typing import Any

from contree_sdk import ContreeSync
from contree_sdk.config import ContreeConfig
from contree_sdk.sdk.exceptions import (
    ApiStatusCodeError,
    ApiTimeoutError,
    ContreeError,
    ContreeTransportError,
    NotFoundError,
    OperationTimedOutError,
    UnprocessableEntityError,
)

from prax_bot.settings import WORKDIR, Settings
from prax_bot.workspace.repository import (
    PATCH_REJECTED,
    apply_patch_shell,
    collect_changes,
    git_exclude_command,
    pack_repo,
    repo_file_names,
    resolve_in_workdir,
    suspicious_files,
)
from prax_bot.workspace.types import Changes, Checkpoint, CommandResult, EventFn, WorkspaceError, noop

UPLOAD_TMP = "/tmp/praxbot.upload"
CLEAN_PATCH = "/tmp/praxbot.clean.patch"
TRANSPORT_TIMEOUT_S = 180.0  # per HTTP request; the SDK default of 10 s fails on multi-megabyte uploads


def make_client() -> ContreeSync:
    """A sandbox client with credentials from the environment or auth.ini and a timeout that survives uploads."""
    return ContreeSync(ContreeConfig(transport_timeout=TRANSPORT_TIMEOUT_S))


class SandboxWorkspace:
    backend = "contree"

    def __init__(
        self,
        client: ContreeSync,
        image: Any,
        *,
        workdir: str = WORKDIR,
        command_timeout: int = 300,
        on_event: EventFn | None = None,
        clean: str | None = None,
    ) -> None:
        self.client = client
        self.image = image
        self.clean = clean
        self._accounts = Lock()  # forks run from other threads and still count
        self.workdir = workdir
        self.command_timeout = command_timeout
        self.on_event = on_event or noop
        self.checkpoints: list[Checkpoint] = []
        self.total_cost = 0.0
        self.commands = 0
        self._checkpoint("baseline")

    @property
    def image_id(self) -> str:
        return str(self.image.uuid)

    @property
    def current_step(self) -> int:
        return self.checkpoints[-1].step

    def _checkpoint(self, label: str) -> Checkpoint:
        cp = Checkpoint(step=len(self.checkpoints), image=self.image_id, label=label[:80], clean=self.clean)
        self.checkpoints.append(cp)
        self.on_event("checkpoint", step=cp.step, image=cp.image, label=cp.label)
        return cp

    def run(
        self,
        command: str,
        *,
        timeout: int | None = None,
        mutate: bool = True,
        truncate_output_at: int | None = None,
        label: str | None = None,
    ) -> CommandResult:
        """Run a shell command in the workdir.

        With mutate=True the resulting filesystem becomes the current state and a checkpoint is
        recorded, labelled with the command or with `label`. With mutate=False the run is disposable.
        """
        timeout = timeout or self.command_timeout
        started = time.monotonic()
        try:
            result_image = self.image.run(
                shell=command,
                cwd=self.workdir,
                timeout=timeout,
                disposable=not mutate,
                truncate_output_at=truncate_output_at,
            ).wait()
        except OperationTimedOutError:
            return self._timed_out(command, timeout, started)
        result = self._account(command, result_image)
        if mutate and result_image.uuid:
            self.image = result_image
            self._checkpoint(label or f"run: {command}")
        return result

    def _timed_out(self, command: str, timeout: int, started: float) -> CommandResult:
        with self._accounts:
            self.commands += 1
        return CommandResult(
            command=command,
            exit_code=124,
            stdout="",
            stderr=f"sandbox operation timed out after {timeout}s",
            elapsed_s=time.monotonic() - started,
            truncated=False,
            timed_out=True,
            image=None,
            cost=0.0,
        )

    def _account(self, command: str, result_image: Any) -> CommandResult:
        """One finished sandbox run as a result, counted into the workspace's commands and cost."""
        res = result_image.result
        raw = getattr(res, "_raw", None)
        timed_out = bool(raw is not None and raw.result is not None and raw.result.state.timed_out)
        with self._accounts:
            self.commands += 1
            self.total_cost += res.cost
        return CommandResult(
            command=command,
            exit_code=result_image.exit_code,
            stdout=str(result_image.stdout or ""),
            stderr=str(result_image.stderr or ""),
            elapsed_s=res.elapsed_time.total_seconds(),
            truncated=res.truncated,
            timed_out=timed_out,
            image=str(result_image.uuid) if result_image.uuid else None,
            cost=res.cost,
        )

    def mark_clean(self) -> None:
        """Declare the current image clean: the repository at its baseline, set up, before any of the agent's work."""
        self.clean = self.image_id
        self.checkpoints[-1].clean = self.clean

    def run_clean(self, command: str, *, patch: str = "", timeout: int | None = None) -> CommandResult:
        """Run a command in a disposable fork of the clean image, with `patch` applied there first.

        The fork holds what the user's tree would hold after an apply and nothing else: no scratch files, no
        packages the agent installed. The workspace and its checkpoints are untouched, so this is safe from
        another thread while the agent works.
        """
        if self.clean is None:
            raise WorkspaceError("this workspace has no clean image to fork")
        timeout = timeout or self.command_timeout
        started = time.monotonic()
        shell = command
        files: dict[str, Any] | None = None
        if patch.strip():
            shell = f"{apply_patch_shell(CLEAN_PATCH)}\n{command}"
            files = {CLEAN_PATCH: patch.encode("utf-8")}
        try:
            forked = (
                self.client.images.use(self.clean)
                .run(shell=shell, cwd=self.workdir, timeout=timeout, disposable=True, files=files)
                .wait()
            )
        except OperationTimedOutError:
            return self._timed_out(command, timeout, started)
        result = self._account(command, forked)
        if result.exit_code == 97 and PATCH_REJECTED in result.stderr:
            raise WorkspaceError(f"the diff does not apply to the clean image: {result.stderr.strip()[-400:]}")
        return result

    def advance_clean(self, patch: str) -> None:
        """The user's tree now holds `patch`, so the clean image gains it. Never fails an apply: if the clean image
        cannot follow, it is forgotten and checks run in the workspace again."""
        if self.clean is None or not patch.strip():
            return
        try:
            advanced = (
                self.client.images.use(self.clean)
                .run(
                    shell=apply_patch_shell(CLEAN_PATCH, skip_if_applied=True),
                    cwd=self.workdir,
                    timeout=120,
                    disposable=False,
                    files={CLEAN_PATCH: patch.encode("utf-8")},
                )
                .wait()
            )
        except ContreeError as error:
            self._forget_clean(f"{type(error).__name__}: {str(error)[:200]}")
            return
        self._account("advance the clean image", advanced)
        if advanced.exit_code != 0 or not advanced.uuid:
            self._forget_clean(str(advanced.stderr or "").strip()[-300:] or "the run produced no image")
            return
        self.clean = str(advanced.uuid)

    def _forget_clean(self, reason: str) -> None:
        self.clean = None
        self.on_event("sandbox.clean_lost", reason=reason)

    def read(self, path: str) -> bytes:
        target = resolve_in_workdir(path, self.workdir)
        try:
            return self.image.read(target)
        except NotFoundError as e:
            raise FileNotFoundError(path) from e
        except UnprocessableEntityError as e:
            raise IsADirectoryError(path) from e

    def write(self, path: str, content: bytes) -> None:
        """Write a file, creating parent directories. One sandbox round trip."""
        target = resolve_in_workdir(path, self.workdir)
        parent = posixpath.dirname(target)
        command = f"mkdir -p {shlex.quote(parent)} && mv -f {UPLOAD_TMP} {shlex.quote(target)}"
        upload: dict[str, Any] = {UPLOAD_TMP: content}
        result_image = self.image.run(
            shell=command,
            files=upload,
            disposable=False,
            timeout=60,
        ).wait()
        self.total_cost += result_image.result.cost
        if result_image.exit_code != 0:
            raise WorkspaceError(f"writing {path} failed: {str(result_image.stderr or '').strip()}")
        self.image = result_image
        self._checkpoint(f"write: {path}")

    def restore(self, step: int) -> Checkpoint:
        if not 0 <= step < len(self.checkpoints):
            raise ValueError(f"no checkpoint {step}; checkpoints run from 0 to {len(self.checkpoints) - 1}")
        target = self.checkpoints[step]
        self.image = self.client.images.use(target.image)
        self._checkpoint(f"restore: step {step}")
        return target

    def changes(self, paths: Sequence[str] | None = None, *, attempts: int = 3) -> Changes:
        """Collect a complete patch; immutable images make failed reads safe to retry."""
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return collect_changes(self, paths)
            except ContreeError as error:
                last = error
                if attempt < attempts - 1:
                    time.sleep(5 * (attempt + 1))
        raise WorkspaceError(
            f"could not read the diff after {attempts} attempts: {type(last).__name__}: {str(last)[:200]}"
        )

    def diff(self) -> str:
        return self.changes().patch


CREATE_ATTEMPTS = 3


def transient(error: Exception) -> bool:
    """A sandbox API failure worth retrying: a dropped connection, a timeout, or a 5xx from the gateway."""
    if isinstance(error, (ContreeTransportError, ApiTimeoutError)):
        return True
    return isinstance(error, ApiStatusCodeError) and int(getattr(error, "status", 0) or 0) >= 500


def with_retries(action: Callable[[], Any], *, label: str, emit: EventFn, attempts: int = CREATE_ATTEMPTS) -> Any:
    for attempt in range(attempts):
        try:
            return action()
        except ContreeError as e:
            if not transient(e) or attempt == attempts - 1:
                raise
            emit("sandbox.retry", step=label, attempt=attempt + 1, error=f"{type(e).__name__}: {str(e)[:160]}")
            time.sleep(5 * (attempt + 1))
    raise AssertionError("unreachable")


def create_workspace(
    client: ContreeSync,
    settings: Settings,
    repo_root: Path,
    on_event: EventFn | None = None,
) -> SandboxWorkspace:
    """Upload the repository into a fresh sandbox image and commit it as the baseline."""
    emit = on_event or noop
    started = time.monotonic()
    archive = pack_repo(repo_root)
    emit("sandbox.pack", bytes=len(archive), suspicious=suspicious_files(repo_file_names(repo_root)))

    base = with_retries(
        lambda: client.images.oci(settings.base_image, timeout=settings.import_timeout), label="base image", emit=emit
    )
    emit("sandbox.base", image=str(base.uuid), tag=base.tag, elapsed=time.monotonic() - started)

    wd = WORKDIR
    bootstrap = (
        f"mkdir -p {wd} && tar -xf /tmp/praxbot-repo.tar -C {wd} && rm -f /tmp/praxbot-repo.tar"
        f" && cd {wd} && git -c init.defaultBranch=main init -q && {git_exclude_command()} && git add -A"
        " && git -c user.email=praxbot@localhost -c user.name=praxbot commit -qm baseline --allow-empty"
    )
    image = with_retries(
        lambda: base.run(
            shell=bootstrap,
            files={"/tmp/praxbot-repo.tar": archive},
            disposable=False,
            timeout=settings.setup_timeout,
        ).wait(),
        label="upload",
        emit=emit,
    )
    if image.exit_code != 0:
        raise WorkspaceError(f"sandbox bootstrap failed (exit {image.exit_code}): {str(image.stderr or '')[-2000:]}")
    ws = SandboxWorkspace(client, image, workdir=wd, command_timeout=settings.command_timeout, on_event=on_event)
    ws.total_cost += image.result.cost
    emit("sandbox.ready", image=ws.image_id, elapsed=time.monotonic() - started)

    if settings.setup:
        r = ws.run(settings.setup, timeout=settings.setup_timeout)
        emit("sandbox.setup", exit_code=r.exit_code, elapsed=r.elapsed_s)
        if r.exit_code != 0:
            raise WorkspaceError(f"setup command failed (exit {r.exit_code}): {(r.stderr or r.stdout)[-2000:]}")
    ws.mark_clean()
    return ws
