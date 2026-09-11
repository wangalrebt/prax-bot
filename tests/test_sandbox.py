from __future__ import annotations

import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from contree_sdk.sdk.exceptions import ContreeError

from prax_bot.workspace.repository import pack_repo
from prax_bot.workspace.sandbox import SandboxWorkspace as Workspace
from prax_bot.workspace.sandbox import create_workspace
from prax_bot.workspace.types import WorkspaceError
from tests.fakes import base, fake_client
from tests.helpers import make_sandbox_repo as make_repo
from tests.helpers import sandbox_settings as settings


def test_pack_repo_includes_tracked_and_untracked_but_not_ignored(tmp_path: Path) -> None:
    data = pack_repo(make_repo(tmp_path))
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        assert sorted(tar.getnames()) == [".gitignore", "a.py", "sub/new.txt"]


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


def test_create_workspace_bootstraps_and_runs_setup(tmp_path: Path) -> None:
    log: list = []
    events: list[str] = []
    ws = create_workspace(
        fake_client(log),
        settings(tmp_path, setup="pip install -e ."),
        make_repo(tmp_path),
        lambda k, **p: events.append(k),
    )
    bootstrap = log[0]
    assert bootstrap["shell"].startswith("mkdir -p /work && tar -xf /tmp/praxbot-repo.tar -C /work")
    assert (
        "git -c init.defaultBranch=main init -q" in bootstrap["shell"] and "commit -qm baseline" in bootstrap["shell"]
    )
    assert ".git/info/exclude" in bootstrap["shell"] and "*.egg-info/" in bootstrap["shell"]
    assert (
        set(bootstrap["files"]) == {"/tmp/praxbot-repo.tar"}
        and bootstrap["disposable"] is False
        and bootstrap["timeout"] == 20
    )
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
    import prax_bot.workspace.sandbox as sb

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
    assert ws.diff().startswith("diff --git") and calls["n"] == 4  # the failed call, then listing, numstat, diff
    ws.image.run = lambda *a, **k: (_ for _ in ()).throw(ContreeError.__new__(ContreeError))
    with pytest.raises(WorkspaceError, match="after 3 attempts"):
        ws.diff()
