"""Verification is explicit evidence, independent of the model's finish message."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from prax_bot.core.tools import ToolBox
from prax_bot.core.verification import CheckResult, verification_status
from prax_bot.runtime.runs import list_runs, load_run
from prax_bot.settings import Settings
from prax_bot.ui.cli import app
from prax_bot.ui.report import render_html
from tests.fakes import FakeLLM, FakeWorkspace, call, reply
from tests.helpers import make_session


def model():
    return FakeLLM(
        [
            reply(None, call("write_file", path="b.py", content="b\n")),
            reply(None, call("finish", summary="done", goal_met=True)),
        ]
    )


def test_checks_record_commands_exit_codes_and_the_tested_image(tmp_path: Path) -> None:
    session = make_session(
        tmp_path, llm=model(), test_command="test -f b.py", lint_command="touch check-artifact; true"
    )
    try:
        result, _ = session.instruct("add b")
        assert result.finished and session.verification_status == "passed"
        messages = session.llm.requests[0][0]
        system = messages[0]["content"]
        assert messages[0]["role"] == "system" and messages[1] == {"role": "user", "content": "add b"}
        assert system.count("- tests: test -f b.py") == 1
        assert system.count("- lint: touch check-artifact; true") == 1
        assert "runs these commands after finish" in system
        assert "No final verification commands are scheduled" not in system
        assert [check.name for check in session.checks] == ["tests", "lint"]
        assert all(check.image == session.ws.image_id for check in session.checks)
        assert not (session.ws.current / "check-artifact").exists(), "verification runs disposably"
        record = json.loads((session.run_dir / "run.json").read_text())
        assert record["verification"]["checks"][0]["command"] == "test -f b.py"
        assert record["verification"]["checks"][0]["exit_code"] == 0
        report = render_html(load_run(session.run_id, session.settings))
        assert "<h2>Verification</h2>" in report and "test -f b.py" in report
        assert f"Tested image: <code>{session.ws.image_id}</code>" in report
        session.ws.write("later.txt", b"more work")
        assert session.verification_status == "stale"
    finally:
        session.close()


def test_failed_check_does_not_become_success_when_the_model_finishes(tmp_path: Path) -> None:
    session = make_session(tmp_path, llm=model(), test_command="echo test-failure; exit 3")
    try:
        result, _ = session.instruct("add b")
        assert result.finished and session.verification_status == "failed"
        assert session.checks[0].exit_code == 3 and "test-failure" in session.checks[0].output
    finally:
        session.close()


def test_goal_reuses_the_configured_check_and_requires_other_checks_to_pass(tmp_path: Path) -> None:
    session = make_session(tmp_path, llm=model(), test_command="test -f b.py", lint_command="exit 1")
    try:
        with patch.object(session.ws, "run", wraps=session.ws.run) as run:
            driver = session.run_goal("add b", max_rounds=1, check="test -f b.py")
        assert not driver.met and session.verification_status == "failed"
        assert sum(call.args[0] == "test -f b.py" for call in run.call_args_list) == 1
        system = session.llm.requests[0][0][0]["content"]
        assert system.count("test -f b.py") == 1 and "- goal:" not in system
    finally:
        session.close()


def test_explicit_goal_check_is_recorded_without_test_configuration(tmp_path: Path) -> None:
    session = make_session(tmp_path, llm=model())
    try:
        driver = session.run_goal("add b", max_rounds=1, check="test -f b.py")
        assert driver.met and session.verification_status == "passed"
        assert "- goal: test -f b.py" in session.llm.requests[0][0][0]["content"]
        assert session.checks[0].name == "goal" and session.checks[0].exit_code == 0
    finally:
        session.close()


def test_only_explicit_passes_from_the_same_command_are_reported_as_fixed() -> None:
    box = ToolBox(FakeWorkspace())
    failed = "FAILED tests/test_bug.py::test_bug - AssertionError\n1 failed"
    first = box._note_test_observations(failed, command="pytest -v", workdir=".")
    assert "1 tests fail before any edit" in first and "tests/test_bug.py::test_bug" in first
    assert not box._note_test_observations("1 passed", command="pytest -v", workdir=".")
    assert not box._note_test_observations("tests/test_bug.py::test_bug PASSED", command="other tests", workdir=".")
    output = box._note_test_observations("tests/test_bug.py::test_bug PASSED [100%]", command="pytest -v", workdir=".")
    assert "explicitly reported passing now: tests/test_bug.py::test_bug" in output


def test_interrupt_during_verification_records_cancellation(tmp_path: Path) -> None:
    session = make_session(tmp_path, llm=model(), test_command="test -f b.py")
    try:
        with patch("prax_bot.runtime.session.run_check", side_effect=KeyboardInterrupt):
            result, diff = session.instruct("add b")
        assert result.reason == "cancelled" and not result.finished
        assert not session.active and "b/b.py" in diff
        assert session.verification_status == "not run"
    finally:
        session.close()


def test_model_error_cannot_complete_a_goal_with_unrun_checks(tmp_path: Path) -> None:
    session = make_session(tmp_path, test_command="exit 1")
    try:
        with (
            patch.object(session.llm, "complete", side_effect=RuntimeError("model unavailable")),
            patch("prax_bot.runtime.session.run_check", side_effect=AssertionError("must not run after error")),
        ):
            driver = session.run_goal("finish", check="true", max_rounds=1)
        assert not driver.met and "error" in driver.stopped
        assert session.last_result.reason == "error"
        assert session.verification_status == "not run"
    finally:
        session.close()


@pytest.mark.parametrize(
    ("checks", "status"),
    [
        ([], "not run"),
        ([CheckResult("tests", "other", "current", 0, "")], "incomplete"),
        ([CheckResult("lint", "pytest", "current", 0, "")], "incomplete"),
        ([CheckResult("tests", "pytest", "old", 0, "")], "stale"),
        ([CheckResult("tests", "pytest", "current", 1, "")], "failed"),
        ([CheckResult("tests", "pytest", "current", 0, "", timed_out=True)], "failed"),
        ([CheckResult("tests", "pytest", "current", 0, "")], "passed"),
    ],
)
def test_verification_requires_matching_current_evidence(checks, status) -> None:
    assert verification_status((("tests", "pytest"),), checks, "current") == status


@pytest.mark.parametrize(("lint_command", "exit_code"), [("true", 0), ("exit 3", 1)])
@pytest.mark.parametrize("apply", [False, True])
def test_cli_verification_flags_control_recorded_checks_and_exit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lint_command: str, exit_code: int, apply: bool
) -> None:
    session = make_session(tmp_path)
    session.close()
    monkeypatch.setenv("PRAXBOT_HOME", str(tmp_path / "cli-home"))
    monkeypatch.setenv("NEBIUS_API_KEY", "test-key")
    monkeypatch.setattr("prax_bot.settings.read_auth_ini", dict)
    monkeypatch.setattr("prax_bot.runtime.factory.open_workspace", lambda *args: session.ws)
    monkeypatch.setattr("prax_bot.core.llm.LLM", lambda settings: model())
    result = CliRunner().invoke(
        app,
        [
            "run",
            "add b",
            "--repo",
            str(session.root),
            "--backend",
            "local",
            "--test-command",
            "test -f b.py",
            "--lint-command",
            lint_command,
            *(["--yes"] if apply else []),
        ],
    )
    assert result.exit_code == exit_code, result.output
    saved = list_runs(Settings.from_env(), limit=1)[0]
    assert saved.info["finished"] and saved.info["state"] == "closed"
    assert saved.info["verification"]["status"] == ("stale" if apply else "passed" if exit_code == 0 else "failed")
    assert (session.root / "b.py").exists() is apply
    assert [check["command"] for check in saved.info["verification"]["checks"]] == ["test -f b.py", lint_command]
