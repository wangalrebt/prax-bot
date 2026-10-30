from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from prax_bot.runtime.sessions import SessionRegistry
from tests.helpers import settings


@pytest.fixture
def registry(tmp_path: Path):
    registry = SessionRegistry(settings(tmp_path), tmp_path / "repo", "local")
    try:
        yield registry
    finally:
        registry.close()


def test_names_and_main_identity_persist_without_selecting_an_existing_run(tmp_path: Path) -> None:
    existing_run = tmp_path / "runs" / "20261029T000000Z-existing"
    existing_run.mkdir(parents=True)
    registry = SessionRegistry(settings(tmp_path), tmp_path / "repo", "local")
    try:
        main_id = registry.main_id
        assert registry.get(main_id)["name"] == "Main"
        assert registry.get(main_id)["run_ids"] == []
        work = registry.create("  Fix login  ", run_id=existing_run.name)
        assert work["name"] == "Fix login"
        renamed = registry.rename(work["id"], "Login follow-up")
        assert renamed["created_at"] == work["created_at"]
        registry.rename(main_id, "Coordinator")
    finally:
        registry.close()

    reopened = SessionRegistry(settings(tmp_path), tmp_path / "repo", "local")
    try:
        assert reopened.main_id == main_id
        assert reopened.get(main_id)["name"] == "Coordinator"
        assert reopened.get(work["id"]) == renamed
        assert reopened.entries()[0]["id"] == main_id
    finally:
        reopened.close()
    assert existing_run.is_dir()


def test_run_lineage_is_ordered_idempotent_and_persistent(registry: SessionRegistry, tmp_path: Path) -> None:
    work = registry.create("Repair", run_id="run-first")
    registry.attach_run(work["id"], "run-second")
    before = registry.get(work["id"])
    registry.attach_run(work["id"], "run-second")
    registry.attach_run(work["id"], "run-first")
    assert registry.get(work["id"]) == before
    assert before["run_ids"] == ["run-first", "run-second"]
    assert before["last_run_id"] == "run-second"
    registry.close()
    reopened = SessionRegistry(settings(tmp_path), tmp_path / "repo", "local")
    try:
        assert reopened.get(work["id"]) == before
    finally:
        reopened.close()


def test_main_is_first_and_other_sessions_sort_by_activity(
    registry: SessionRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("prax_bot.runtime.sessions.time.time", lambda: 10.0)
    first = registry.create("First")
    monkeypatch.setattr("prax_bot.runtime.sessions.time.time", lambda: 20.0)
    second = registry.create("Second")
    assert [entry["id"] for entry in registry.entries()] == [registry.main_id, second["id"], first["id"]]
    monkeypatch.setattr("prax_bot.runtime.sessions.time.time", lambda: 30.0)
    registry.attach_run(first["id"], "new-run")
    assert [entry["id"] for entry in registry.entries()] == [registry.main_id, first["id"], second["id"]]


def test_repository_and_backend_have_separate_manifests(tmp_path: Path) -> None:
    s = settings(tmp_path)
    registries = [
        SessionRegistry(s, tmp_path / "repo", "local"),
        SessionRegistry(s, tmp_path / "other", "local"),
        SessionRegistry(s, tmp_path / "repo", "contree"),
    ]
    try:
        assert len({registry.main_id for registry in registries}) == 3
        assert len(list((tmp_path / "sessions").glob("*.json"))) == 3
        registries[0].create("Only in first")
        assert [len(registry.entries()) for registry in registries] == [2, 1, 1]
    finally:
        for registry in registries:
            registry.close()


def test_resolved_repository_identity_and_competing_manager_refusal(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    alias = tmp_path / "repo-link"
    alias.symlink_to(root, target_is_directory=True)
    s = settings(tmp_path)
    registry = SessionRegistry(s, root, "local")
    main_id = registry.main_id
    try:
        for competing_root in (root, alias, root / ".." / "repo"):
            with pytest.raises(RuntimeError, match="already open"):
                SessionRegistry(s, competing_root, "local")
        assert registry.get(main_id)["is_main"] is True
        lock_path = next((tmp_path / "sessions").glob("*.lock"))
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import fcntl, sys\n"
                "with open(sys.argv[1], 'a') as lock:\n"
                "    try:\n"
                "        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "    except BlockingIOError:\n"
                "        sys.exit(0)\n"
                "    sys.exit(1)\n",
                str(lock_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
    finally:
        registry.close()
        registry.close()
    reopened = SessionRegistry(s, alias, "local")
    try:
        assert reopened.main_id == main_id
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "damage",
    [
        "invalid-json",
        "missing-main",
        "duplicate-session",
        "wrong-root",
        "wrong-backend",
        "wrong-main-flag",
        "invalid-name",
        "invalid-run",
        "inconsistent-lineage",
        "invalid-timestamp",
    ],
)
def test_corrupt_manifest_is_preserved_and_releases_lock(tmp_path: Path, damage: str) -> None:
    s = settings(tmp_path)
    registry = SessionRegistry(s, tmp_path / "repo", "local")
    main_id = registry.main_id
    registry.close()
    path = next((tmp_path / "sessions").glob("*.json"))
    original = path.read_text()
    data = json.loads(original)
    match damage:
        case "missing-main":
            data["sessions"] = []
        case "duplicate-session":
            data["sessions"].append(data["sessions"][0])
        case "wrong-root":
            data["root"] = "other"
        case "wrong-backend":
            data["backend"] = "other"
        case "wrong-main-flag":
            data["sessions"][0]["is_main"] = False
        case "invalid-name":
            data["sessions"][0]["name"] = " "
        case "invalid-run":
            data["sessions"][0]["run_ids"] = ["../escape"]
        case "inconsistent-lineage":
            data["sessions"][0]["last_run_id"] = "missing"
        case "invalid-timestamp":
            data["sessions"][0]["updated_at"] = "yesterday"
    damaged = "{" if damage == "invalid-json" else json.dumps(data)
    path.write_text(damaged)
    with pytest.raises(ValueError, match="invalid session manifest"):
        SessionRegistry(s, tmp_path / "repo", "local")
    assert path.read_text() == damaged
    path.write_text(original)
    reopened = SessionRegistry(s, tmp_path / "repo", "local")
    try:
        assert reopened.main_id == main_id
    finally:
        reopened.close()


@pytest.mark.parametrize("name", ["", " ", "\n\t", "a" * 121, None, 42])
def test_invalid_names_do_not_change_registry(registry: SessionRegistry, name: str) -> None:
    before = registry.entries()
    with pytest.raises(ValueError, match="session name"):
        registry.create(name)
    with pytest.raises(ValueError, match="session name"):
        registry.rename(registry.main_id, name)
    assert registry.entries() == before


def test_name_at_limit_and_duplicate_display_names_are_allowed(registry: SessionRegistry) -> None:
    first = registry.create("a" * 120)
    second = registry.create(first["name"])
    assert first["id"] != second["id"]


@pytest.mark.parametrize("run_id", ["", ".", "..", "../run", "a/b", "/absolute", "a\\b", "run\0", 42])
def test_invalid_run_ids_do_not_change_registry(registry: SessionRegistry, run_id: str) -> None:
    before = registry.entries()
    with pytest.raises(ValueError, match="run ID"):
        registry.create("Bad run", run_id=run_id)
    with pytest.raises(ValueError, match="run ID"):
        registry.attach_run(registry.main_id, run_id)
    assert registry.entries() == before


def test_unknown_session_errors(registry: SessionRegistry) -> None:
    for action in (
        lambda: registry.get("missing"),
        lambda: registry.rename("missing", "Name"),
        lambda: registry.attach_run("missing", "run"),
    ):
        with pytest.raises(ValueError, match="unknown session"):
            action()


def test_snapshots_do_not_share_mutable_state(registry: SessionRegistry) -> None:
    created = registry.create("Original", run_id="first")
    session_id = created["id"]
    created["run_ids"].append("unrecorded")
    created["name"] = "Changed"
    snapshot = registry.get(session_id)
    snapshot["run_ids"].clear()
    renamed = registry.rename(session_id, "Renamed")
    renamed["run_ids"].append("unrecorded")
    entries = registry.entries()
    entries[1]["run_ids"].clear()
    assert registry.get(session_id)["run_ids"] == ["first"]
    assert registry.get(session_id)["name"] == "Renamed"


def test_concurrent_updates_preserve_every_run(registry: SessionRegistry) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda number: registry.attach_run(registry.main_id, f"run-{number}"), range(20)))
    assert set(registry.get(registry.main_id)["run_ids"]) == {f"run-{number}" for number in range(20)}


def test_write_failure_does_not_update_memory(registry: SessionRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    before = registry.entries()

    def fail_write(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("prax_bot.runtime.sessions.write_json", fail_write)
    for action in (
        lambda: registry.create("Unwritten"),
        lambda: registry.rename(registry.main_id, "Unwritten"),
        lambda: registry.attach_run(registry.main_id, "unwritten"),
    ):
        with pytest.raises(OSError, match="disk full"):
            action()
        assert registry.entries() == before


def test_constructor_write_failure_releases_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_write(*args: object) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr("prax_bot.runtime.sessions.write_json", fail_write)
        with pytest.raises(OSError, match="disk full"):
            SessionRegistry(settings(tmp_path), tmp_path / "repo", "local")
    registry = SessionRegistry(settings(tmp_path), tmp_path / "repo", "local")
    registry.close()


def test_closed_registry_cannot_keep_reading_or_writing(registry: SessionRegistry) -> None:
    registry.close()
    for action in (
        registry.entries,
        lambda: registry.get(registry.main_id),
        lambda: registry.create("Closed"),
        lambda: registry.rename(registry.main_id, "Closed"),
        lambda: registry.attach_run(registry.main_id, "closed"),
    ):
        with pytest.raises(RuntimeError, match="closed"):
            action()
