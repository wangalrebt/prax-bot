"""The repository inside a Token Factory Sandbox, tracked as a chain of immutable images.

Every mutating command produces a new image and therefore a checkpoint. Read-only
commands run disposably and leave the current image untouched.
"""

from __future__ import annotations

import io
import posixpath
import re
import shlex
import subprocess
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
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

EventFn = Callable[..., None]
MAX_REPO_BYTES = 200 * 1024 * 1024
UPLOAD_TMP = "/tmp/praxbot.upload"
DIFF_OUTPUT_LIMIT = 8 * 1024 * 1024  # the sandbox API rejects truncate_output_at of 10 MiB or more

# Build artifacts that installs and test runs create inside the workspace. They are kept out
# of diffs through .git/info/exclude, which lives outside the working tree.
DIFF_EXCLUDES = (
    "__pycache__/",
    "*.pyc",
    "*.egg-info/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "build/",
    "dist/",
    ".venv/",
    "venv/",
    "node_modules/",
    ".tox/",
    ".coverage",
    "htmlcov/",
)


def git_exclude_command() -> str:
    """Shell snippet that appends DIFF_EXCLUDES to .git/info/exclude in the current directory."""
    body = "\\n".join(DIFF_EXCLUDES)
    return f"mkdir -p .git/info && printf '%b\\n' '{body}' >> .git/info/exclude"


# Untracked files that are commonly secrets. They are not ignored by git, so they would be
# uploaded with the repository; the CLI warns before that happens.
SUSPICIOUS_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "*.keystore",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "credentials*.json",
    "service-account*.json",
)


TRANSPORT_TIMEOUT_S = 180.0  # per HTTP request; the SDK default of 10 s fails on multi-megabyte uploads


def make_client() -> ContreeSync:
    """A sandbox client with credentials from the environment or auth.ini and a timeout that survives uploads."""
    return ContreeSync(ContreeConfig(transport_timeout=TRANSPORT_TIMEOUT_S))


def _noop(kind: str, **payload: Any) -> None:
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


SCRATCH_DIR = "/tmp/praxbot/scratch"  # throwaway scripts live here, outside the repository and the diff
SCRATCH_NAME_RE = re.compile(r"^(reproduce|repro|scratch|debug|tmp|temp)[^/]*$|^test_[^/]*\.py$|^[^/]*_test\.py$")


def resolve_in_workdir(path: str, workdir: str = WORKDIR) -> str:
    """Map a model-supplied path onto an absolute path inside the sandbox workdir or the scratch dir.

    Raises ValueError for paths that would escape both.
    """
    raw = (path or ".").strip()
    candidate = posixpath.normpath(raw if raw.startswith("/") else posixpath.join(workdir, raw))
    for root in (workdir, SCRATCH_DIR):
        if candidate == root or candidate.startswith(root + "/"):
            return candidate
    raise ValueError(f"path {path!r} is outside the repository ({workdir}); throwaway files belong in {SCRATCH_DIR}")


def scratch_files(new_paths: list[str]) -> list[str]:
    """New repository-root files that look like reproduction or debugging scripts."""
    return [p for p in new_paths if "/" not in p and SCRATCH_NAME_RE.match(p)]


def repo_file_names(repo_root: Path) -> list[str]:
    """Tracked files plus untracked files that are not ignored, as git sees them."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "-co", "--exclude-standard"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    ).stdout
    return [n for n in out.decode("utf-8", "surrogateescape").split("\0") if n]


def suspicious_files(names: list[str]) -> list[str]:
    """Names that look like credentials, matched on the file's basename."""
    import fnmatch

    found = []
    for name in names:
        base = posixpath.basename(name)
        if any(fnmatch.fnmatch(base, pat) for pat in SUSPICIOUS_PATTERNS):
            found.append(name)
    return found


def pack_repo(repo_root: Path) -> bytes:
    """Tar the working tree: tracked files plus untracked files that are not ignored.

    The .git directory is not included; the sandbox creates its own baseline commit.
    """
    names = repo_file_names(repo_root)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in names:
            p = repo_root / name
            if p.is_symlink() or p.is_file():
                tar.add(p, arcname=name, recursive=False)
    data = buf.getvalue()
    if len(data) > MAX_REPO_BYTES:
        raise WorkspaceError(
            f"repository is {len(data) / 1e6:.0f} MB; Prax Bot uploads at most {MAX_REPO_BYTES / 1e6:.0f} MB"
        )
    return data


class Workspace:
    def __init__(
        self,
        client: ContreeSync,
        image: Any,
        *,
        workdir: str = WORKDIR,
        command_timeout: int = 300,
        on_event: EventFn | None = None,
    ) -> None:
        self.client = client
        self.image = image
        self.workdir = workdir
        self.command_timeout = command_timeout
        self.on_event = on_event or _noop
        self.checkpoints: list[Checkpoint] = []
        self.total_cost = 0.0
        self.commands = 0
        self.dropped_scratch: list[str] = []
        self._checkpoint("baseline")

    @property
    def image_id(self) -> str:
        return str(self.image.uuid)

    @property
    def current_step(self) -> int:
        return self.checkpoints[-1].step

    def _checkpoint(self, label: str) -> Checkpoint:
        cp = Checkpoint(step=len(self.checkpoints), image=self.image_id, label=label[:80])
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
    ) -> CommandResult:
        """Run a shell command in the workdir.

        With mutate=True the resulting filesystem becomes the current state and a
        checkpoint is recorded. With mutate=False the run is disposable.
        """
        timeout = timeout or self.command_timeout
        started = time.monotonic()
        self.commands += 1
        try:
            result_image = self.image.run(
                shell=command,
                cwd=self.workdir,
                timeout=timeout,
                disposable=not mutate,
                truncate_output_at=truncate_output_at,
            ).wait()
        except OperationTimedOutError:
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
        res = result_image.result
        raw = getattr(res, "_raw", None)
        timed_out = bool(raw is not None and raw.result is not None and raw.result.state.timed_out)
        self.total_cost += res.cost
        image_id = str(result_image.uuid) if result_image.uuid else None
        if mutate and result_image.uuid:
            self.image = result_image
            self._checkpoint(f"run: {command}")
        return CommandResult(
            command=command,
            exit_code=result_image.exit_code,
            stdout=str(result_image.stdout or ""),
            stderr=str(result_image.stderr or ""),
            elapsed_s=res.elapsed_time.total_seconds(),
            truncated=res.truncated,
            timed_out=timed_out,
            image=image_id,
            cost=res.cost,
        )

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
        result_image = self.image.run(
            shell=command,
            files={UPLOAD_TMP: content},
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

    def diff(self, *, attempts: int = 3) -> str:
        """Unified diff of the current state against the baseline commit, new files included.

        The current image is immutable, so a transient API failure here is retried rather
        than losing the run's work.
        """
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                listing = self.run("git ls-files --others --exclude-standard", mutate=False)
                self.dropped_scratch = scratch_files(listing.stdout.splitlines()) if listing.exit_code == 0 else []
                excludes = " ".join(shlex.quote(f":(exclude){p}") for p in self.dropped_scratch)
                r = self.run(
                    f"git add -A && git diff --cached --binary HEAD -- . {excludes}",
                    mutate=False,
                    truncate_output_at=DIFF_OUTPUT_LIMIT,
                )
                break
            except ContreeError as e:
                last = e
                time.sleep(5 * (attempt + 1))
        else:
            raise WorkspaceError(
                f"could not read the diff after {attempts} attempts: {type(last).__name__}: {str(last)[:200]}"
            )
        if r.exit_code != 0:
            raise WorkspaceError(f"git diff failed in the sandbox: {r.stderr.strip()}")
        if r.truncated:
            raise WorkspaceError(f"the diff is larger than {DIFF_OUTPUT_LIMIT // (1024 * 1024)} MiB and was truncated")
        return r.stdout


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
) -> Workspace:
    """Upload the repository into a fresh sandbox image and commit it as the baseline."""
    emit = on_event or _noop
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
    ws = Workspace(client, image, workdir=wd, command_timeout=settings.command_timeout, on_event=on_event)
    ws.total_cost += image.result.cost
    emit("sandbox.ready", image=ws.image_id, elapsed=time.monotonic() - started)

    if settings.setup:
        r = ws.run(settings.setup, timeout=settings.setup_timeout)
        emit("sandbox.setup", exit_code=r.exit_code, elapsed=r.elapsed_s)
        if r.exit_code != 0:
            raise WorkspaceError(f"setup command failed (exit {r.exit_code}): {(r.stderr or r.stdout)[-2000:]}")
    return ws
