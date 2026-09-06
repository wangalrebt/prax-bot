from __future__ import annotations

import io
import subprocess
import tarfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from contree_sdk.sdk.exceptions import ContreeError, NotFoundError

from prax_bot.sandbox import Workspace, WorkspaceError, create_workspace, pack_repo
from prax_bot.settings import Settings


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_repo(root: Path) -> Path:
    git("init", "-q", cwd=root)
    (root / "a.py").write_text("print(1)\n")
    (root / ".gitignore").write_text("ignored.txt\n")
    (root / "ignored.txt").write_text("no")
    (root / "sub").mkdir()
    (root / "sub" / "new.txt").write_text("untracked")
    git("add", "a.py", ".gitignore", cwd=root)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=root)
    return root


def test_pack_repo_includes_tracked_and_untracked_but_not_ignored(tmp_path: Path) -> None:
    data = pack_repo(make_repo(tmp_path))
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        assert sorted(tar.getnames()) == [".gitignore", "a.py", "sub/new.txt"]


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
        child._pending = {"shell": shell, "cwd": cwd, "timeout": timeout, "disposable": disposable, "files": dict(files or {}), "truncate_output_at": truncate_output_at}
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
        if shell.startswith("git add -A && git diff"):
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


def make_workspace(log: list) -> Workspace:
    return Workspace(fake_client(log), base(log), command_timeout=30)


def test_mutating_run_advances_the_image_and_checkpoints() -> None:
    log: list = []
    ws = make_workspace(log)
    before = ws.image_id
    r = ws.run("touch marker")
    assert r.exit_code == 0 and r.image == ws.image_id and ws.image_id != before
    assert [c.label for c in ws.checkpoints] == ["baseline", "run: touch marker"]
    assert ws.read("marker") == b""
    assert log[-1]["cwd"] == "/work" and log[-1]["disposable"] is False and log[-1]["timeout"] == 30


def test_readonly_run_leaves_the_image_alone() -> None:
    log: list = []
    ws = make_workspace(log)
    before = ws.image_id
    r = ws.run("ls", mutate=False, timeout=5)
    assert r.stdout == "ok\n" and r.image is None
    assert ws.image_id == before and len(ws.checkpoints) == 1
    assert log[-1]["disposable"] is True and log[-1]["timeout"] == 5


def test_write_uploads_then_moves_into_place() -> None:
    log: list = []
    ws = make_workspace(log)
    ws.write("src/new.py", b"x = 1\n")
    assert ws.read("src/new.py") == b"x = 1\n"
    assert ws.checkpoints[-1].label == "write: src/new.py"
    assert log[-1]["files"] == {"/tmp/praxbot.upload": b"x = 1\n"}
    assert log[-1]["shell"] == "mkdir -p /work/src && mv -f /tmp/praxbot.upload /work/src/new.py"


def test_restore_returns_to_an_earlier_state() -> None:
    log: list = []
    ws = make_workspace(log)
    ws.run("touch marker")
    assert ws.read("marker") == b""
    target = ws.restore(0)
    assert target.label == "baseline" and ws.image_id == ws.checkpoints[0].image
    assert ws.checkpoints[-1].label == "restore: step 0"
    with pytest.raises(FileNotFoundError):
        ws.read("marker")
    with pytest.raises(ValueError):
        ws.restore(99)


def test_diff_runs_git_in_the_sandbox_without_advancing() -> None:
    log: list = []
    ws = make_workspace(log)
    before = ws.image_id
    assert ws.diff().startswith("diff --git")
    assert ws.image_id == before and log[-1]["disposable"] is True
    assert 0 < log[-1]["truncate_output_at"] < 10 * 1024 * 1024


def test_paths_cannot_escape_the_workdir() -> None:
    ws = make_workspace([])
    with pytest.raises(ValueError):
        ws.read("../etc/passwd")
    with pytest.raises(ValueError):
        ws.write("/etc/cron.d/x", b"")


def settings(tmp_path: Path, setup: str | None = None) -> Settings:
    return Settings(
        api_key="k", project_id="p", model="m", llm_base_url="u", base_image="python:3.12", setup=setup,
        max_steps=5, command_timeout=10, setup_timeout=20, import_timeout=30, home=tmp_path,
    )


def test_create_workspace_bootstraps_and_runs_setup(tmp_path: Path) -> None:
    log: list = []
    events: list[str] = []
    ws = create_workspace(fake_client(log), settings(tmp_path, setup="pip install -e ."), make_repo(tmp_path), lambda k, **p: events.append(k))
    bootstrap = log[0]
    assert bootstrap["shell"].startswith("mkdir -p /work && tar -xf /tmp/praxbot-repo.tar -C /work")
    assert "git -c init.defaultBranch=main init -q" in bootstrap["shell"] and "commit -qm baseline" in bootstrap["shell"]
    assert ".git/info/exclude" in bootstrap["shell"] and "*.egg-info/" in bootstrap["shell"]
    assert set(bootstrap["files"]) == {"/tmp/praxbot-repo.tar"} and bootstrap["disposable"] is False and bootstrap["timeout"] == 20
    assert log[1]["shell"] == "pip install -e ." and log[1]["timeout"] == 20
    assert events[:4] == ["sandbox.pack", "sandbox.base", "checkpoint", "sandbox.ready"] and "sandbox.setup" in events
    assert ws.checkpoints[0].label == "baseline" and ws.checkpoints[1].label == "run: pip install -e ."


def test_create_workspace_reports_bootstrap_failure(tmp_path: Path) -> None:
    log: list = []
    broken = base(log)
    broken.fail_bootstrap = True
    client = SimpleNamespace(images=SimpleNamespace(oci=lambda ref, timeout=None: broken, use=None))
    with pytest.raises(WorkspaceError, match="bootstrap failed"):
        create_workspace(client, settings(tmp_path), make_repo(tmp_path))


def test_diff_retries_transient_sandbox_errors(monkeypatch) -> None:
    import prax_bot.sandbox as sb

    monkeypatch.setattr(sb.time, "sleep", lambda s: None)
    log: list = []
    ws = make_workspace(log)
    calls = {"n": 0}
    original = ws.image.run

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ContreeError.__new__(ContreeError)
        return original(*a, **k)

    ws.image.run = flaky
    assert ws.diff().startswith("diff --git") and calls["n"] == 2
    ws.image.run = lambda *a, **k: (_ for _ in ()).throw(ContreeError.__new__(ContreeError))
    with pytest.raises(WorkspaceError, match="after 3 attempts"):
        ws.diff()
