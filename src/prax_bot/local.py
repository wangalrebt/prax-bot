"""A local stand-in for the sandbox: the same workspace interface over a temporary directory.

Commands run on this machine with a scrubbed environment and a private virtualenv.
Checkpoints are directory clones. This is a development and fallback backend, not isolation.
"""

from __future__ import annotations

import io
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from .sandbox import (
    DIFF_EXCLUDES,
    DIFF_OUTPUT_LIMIT,
    SCRATCH_DIR,
    Checkpoint,
    CommandResult,
    EventFn,
    WorkspaceError,
    _noop,
    pack_repo,
    repo_file_names,
    resolve_in_workdir,
    scratch_files,
    suspicious_files,
)
from .settings import WORKDIR, Settings

DEFAULT_TRUNCATE = 65_535


def clone_dir(src: Path, dst: Path) -> None:
    """Copy a directory tree. On macOS this uses APFS clonefile, which is instant."""
    if sys.platform == "darwin":
        r = subprocess.run(["cp", "-c", "-R", str(src), str(dst)], capture_output=True)
        if r.returncode == 0:
            return
    shutil.copytree(src, dst, symlinks=True)


class LocalWorkspace:
    def __init__(self, root: Path, *, command_timeout: int = 300, on_event: EventFn | None = None) -> None:
        self.root = root
        self.current = root / "current"
        self.snapshots = root / "snapshots"
        self.venv = root / "venv"
        self.workdir = WORKDIR
        self.command_timeout = command_timeout
        self.on_event = on_event or _noop
        self.checkpoints: list[Checkpoint] = []
        self.total_cost = 0.0
        self.commands = 0
        self.dropped_scratch: list[str] = []
        for d in (self.snapshots, root / "home", root / "tmp"):
            d.mkdir(parents=True, exist_ok=True)
        self._checkpoint("baseline")

    @property
    def image_id(self) -> str:
        return self.checkpoints[-1].image

    @property
    def current_step(self) -> int:
        return self.checkpoints[-1].step

    def _checkpoint(self, label: str) -> Checkpoint:
        n = len(self.checkpoints)
        clone_dir(self.current, self.snapshots / f"snap-{n}")
        cp = Checkpoint(step=n, image=f"local:snap-{n}", label=label[:80])
        self.checkpoints.append(cp)
        self.on_event("checkpoint", step=cp.step, image=cp.image, label=cp.label)
        return cp

    def _local_path(self, path: str) -> Path:
        absolute = resolve_in_workdir(path, self.workdir)
        if absolute.startswith(SCRATCH_DIR):
            return self.root / "tmp" / "praxbot" / "scratch" / absolute[len(SCRATCH_DIR) :].lstrip("/")
        return self.current / absolute[len(self.workdir) :].lstrip("/")

    def _env(self) -> dict[str, str]:
        return {
            "PATH": f"{self.venv / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "VIRTUAL_ENV": str(self.venv),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
        }

    def run(
        self,
        command: str,
        *,
        timeout: int | None = None,
        mutate: bool = True,
        truncate_output_at: int | None = None,
    ) -> CommandResult:
        timeout = timeout or self.command_timeout
        limit = truncate_output_at or DEFAULT_TRUNCATE
        self.commands += 1
        scratch: Path | None = None
        cwd = self.current
        if not mutate:
            scratch = self.root / "tmp" / f"ro-{time.time_ns()}"
            clone_dir(self.current, scratch)
            cwd = scratch
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                ["bash", "-c", command],
                cwd=cwd,
                env=self._env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                out, err = proc.communicate(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                out, err = proc.communicate()
                timed_out = True
        finally:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
        elapsed = time.monotonic() - started
        truncated = len(out) > limit or len(err) > limit
        if mutate:
            self._checkpoint(f"run: {command}")
        return CommandResult(
            command=command,
            exit_code=124 if timed_out else proc.returncode,
            stdout=out[:limit].decode("utf-8", "replace"),
            stderr=err[:limit].decode("utf-8", "replace"),
            elapsed_s=elapsed,
            truncated=truncated,
            timed_out=timed_out,
            image=self.image_id if mutate else None,
            cost=0.0,
        )

    def read(self, path: str) -> bytes:
        p = self._local_path(path)
        if p.is_dir():
            raise IsADirectoryError(path)
        if not p.is_file():
            raise FileNotFoundError(path)
        return p.read_bytes()

    def write(self, path: str, content: bytes) -> None:
        p = self._local_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        self._checkpoint(f"write: {path}")

    def restore(self, step: int) -> Checkpoint:
        if not 0 <= step < len(self.checkpoints):
            raise ValueError(f"no checkpoint {step}; checkpoints run from 0 to {len(self.checkpoints) - 1}")
        target = self.checkpoints[step]
        shutil.rmtree(self.current)
        clone_dir(self.snapshots / f"snap-{step}", self.current)
        self._checkpoint(f"restore: step {step}")
        return target

    def diff(self) -> str:
        listing = self.run("git ls-files --others --exclude-standard", mutate=False)
        self.dropped_scratch = scratch_files(listing.stdout.splitlines()) if listing.exit_code == 0 else []
        excludes = " ".join(shlex.quote(f":(exclude){p}") for p in self.dropped_scratch)
        r = self.run(
            f"git add -A && git diff --cached --binary HEAD -- . {excludes}",
            mutate=False,
            truncate_output_at=DIFF_OUTPUT_LIMIT,
        )
        if r.exit_code != 0:
            raise WorkspaceError(f"git diff failed: {r.stderr.strip()}")
        if r.truncated:
            raise WorkspaceError(f"the diff is larger than {DIFF_OUTPUT_LIMIT // (1024 * 1024)} MiB and was truncated")
        return r.stdout


def create_local_workspace(
    settings: Settings,
    repo_root: Path,
    on_event: EventFn | None = None,
    run_id: str = "dev",
) -> LocalWorkspace:
    emit = on_event or _noop
    started = time.monotonic()
    root = settings.home / "local" / run_id
    if root.exists():
        shutil.rmtree(root)
    current = root / "current"
    current.mkdir(parents=True)

    archive = pack_repo(repo_root)
    emit("sandbox.pack", bytes=len(archive), suspicious=suspicious_files(repo_file_names(repo_root)))
    try:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(current, filter="data")
    except tarfile.FilterError as e:
        raise WorkspaceError(f"the repository contains an entry the local backend refuses to extract: {e}") from None

    git_env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    r = subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "-q"], cwd=current, capture_output=True, text=True, env=git_env
    )
    if r.returncode != 0:
        raise WorkspaceError(f"local baseline failed: git init: {r.stderr.strip()}")
    exclude = current / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as f:
        f.write("\n".join(DIFF_EXCLUDES) + "\n")
    for args in (
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.email=praxbot@localhost",
            "-c",
            "user.name=praxbot",
            "commit",
            "-qm",
            "baseline",
            "--allow-empty",
        ],
    ):
        r = subprocess.run(args, cwd=current, capture_output=True, text=True, env=git_env)
        if r.returncode != 0:
            raise WorkspaceError(f"local baseline failed: {' '.join(args)}: {r.stderr.strip()}")

    uv = shutil.which("uv")
    if uv is None:
        raise WorkspaceError("the local backend needs uv on PATH to create a private virtualenv")
    r = subprocess.run([uv, "venv", "--seed", "--python", "3.12", str(root / "venv")], capture_output=True, text=True)
    if r.returncode != 0:
        raise WorkspaceError(f"creating the virtualenv failed: {r.stderr.strip()[-500:]}")
    emit("sandbox.base", image="local:venv", tag="python 3.12 virtualenv", elapsed=time.monotonic() - started)

    ws = LocalWorkspace(root, command_timeout=settings.command_timeout, on_event=on_event)
    emit("sandbox.ready", image=ws.image_id, elapsed=time.monotonic() - started)

    if settings.setup:
        res = ws.run(settings.setup, timeout=settings.setup_timeout)
        emit("sandbox.setup", exit_code=res.exit_code, elapsed=res.elapsed_s)
        if res.exit_code != 0:
            raise WorkspaceError(f"setup command failed (exit {res.exit_code}): {(res.stderr or res.stdout)[-2000:]}")
    return ws
