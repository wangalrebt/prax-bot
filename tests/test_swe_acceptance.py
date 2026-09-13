"""Executable acceptance evidence and phase isolation, without model or sandbox API calls."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from scripts.swe_acceptance import CheckSpec, build_check, execute_check, run_acceptance_experiment

from prax_bot.core.verification import CheckResult
from prax_bot.workspace.local import LocalWorkspace
from tests.fakes import FakeLLM, call, reply
from tests.helpers import git_repo, settings


def workspace(root: Path) -> LocalWorkspace:
    root.mkdir()
    git_repo(root / "current")
    ws = LocalWorkspace(root)
    ws.venv = Path(sys.executable).parent.parent
    return ws


@pytest.mark.parametrize(
    "setup,expression,status",
    [
        ("value = 1", "value == 1", "passed"),
        ("value = 1", "value == 2", "failed"),
        ("import prax_nonexistent_dependency", "True", "error"),
        ("raise AssertionError('setup failure')", "True", "error"),
        ("", "1 / 0 == 0", "error"),
        ("", "1", "error"),
        ("import sys; sys.exit(0)", "True", "error"),
        ("import sys; sys.stderr.write('diagnostic\\n')", "False", "failed"),
    ],
)
def test_only_observed_boolean_assertions_produce_pass_or_failure(
    tmp_path: Path, setup: str, expression: str, status: str
) -> None:
    ws = workspace(tmp_path / "ws")
    frozen = build_check(CheckSpec(requirement="actual behavior", setup=setup, assertions=[expression]))
    observed = execute_check(ws, frozen, "test")
    assert observed.status == status
    assert observed.execution.image == ws.image_id


def test_expected_exception_can_be_asserted_and_check_execution_is_disposable(tmp_path: Path) -> None:
    ws = workspace(tmp_path / "ws")
    spec = CheckSpec(
        requirement="division rejects zero",
        setup="from pathlib import Path\nPath('artifact').write_text('temporary')\ntry:\n    1/0\nexcept ZeroDivisionError:\n    rejected = True",
        assertions=["rejected is True"],
    )
    frozen = build_check(spec)
    image = ws.image_id
    assert execute_check(ws, frozen, "test").status == "passed"
    assert not (ws.current / "artifact").exists() and ws.image_id == image
    assert build_check(spec).command == frozen.command and build_check(spec).sha256 == frozen.sha256
    with pytest.raises((TypeError, ValidationError)):
        frozen.spec.assertions[0] = "False"


def test_invalid_syntax_is_rejected_before_execution() -> None:
    with pytest.raises(SyntaxError):
        build_check(CheckSpec(requirement="x", setup="x =", assertions=["True"]))
    with pytest.raises(SyntaxError):
        build_check(CheckSpec(requirement="x", setup="", assertions=["x = 1"]))


@pytest.mark.parametrize(
    "exit_code,output,timed_out",
    [
        (1, "AssertionError: an unrelated command failed", False),
        (0, 'PRAXBOT_ACCEPTANCE_RESULT:{"status":"failed","failed":[0]}', False),
        (0, 'PRAXBOT_ACCEPTANCE_RESULT:{"status":"passed","failed":[]}\ntrailing output', False),
        (0, 'PRAXBOT_ACCEPTANCE_RESULT:{"status":"passed","failed":[]}', True),
    ],
)
def test_generic_failure_bad_protocol_and_timeout_are_inconclusive(
    monkeypatch: pytest.MonkeyPatch, exit_code: int, output: str, timed_out: bool
) -> None:
    frozen = build_check(CheckSpec(requirement="x", setup="", assertions=["False"]))
    monkeypatch.setattr(
        "scripts.swe_acceptance.run_check",
        lambda *args: CheckResult("x", frozen.command, "image", exit_code, output, timed_out=timed_out),
    )
    assert execute_check(object(), frozen, "test").status == "error"


def test_author_edits_are_isolated_and_exact_check_drives_one_repair(tmp_path: Path) -> None:
    author, original, solver = (workspace(tmp_path / name) for name in ("author", "original", "solver"))
    out = tmp_path / "out"
    out.mkdir()
    spec = dict(
        requirement="a.py must contain z",
        setup="from pathlib import Path\nvalue = Path('a.py').read_text()",
        assertions=["value == 'z\\n'"],
    )
    llm = FakeLLM(
        [
            reply(None, call("read_file", path="a.py")),
            reply(None, call("edit_file", path="a.py", old="x", new="author-only")),
            reply(None, call("submit_acceptance_check", **spec)),
            reply(None, call("finish", summary="check accepted")),
            reply(None, call("read_file", path="a.py")),
            reply(None, call("edit_file", path="a.py", old="x", new="y")),
            reply(None, call("finish", summary="first fix")),
            reply(None, call("read_file", path="a.py")),
            reply(None, call("edit_file", path="a.py", old="y", new="z")),
            reply(None, call("finish", summary="repaired")),
        ]
    )
    config = settings(tmp_path)
    config.max_steps = 20
    events: list[dict] = []
    result, record = run_acceptance_experiment(
        llm,
        solver,
        author_workspace=author,
        baseline_workspace=original,
        task="fix a.py",
        output_dir=out,
        settings=config,
        protected=(),
        on_event=lambda kind, **p: events.append({"kind": kind, **p}),
    )
    assert result.finished and result.steps == 10 and result.summary == "repaired"
    assert author.read("a.py") == b"author-only\n" and original.read("a.py") == b"x\n" and solver.read("a.py") == b"z\n"
    assert [record[key]["status"] for key in ("solver_check", "repair_check")] == ["failed", "passed"]
    observations = [event for event in events if event["kind"] == "acceptance.check"]
    assert [event["status"] for event in observations] == ["failed", "failed", "passed"]
    assert len({event["execution"]["command"] for event in observations}) == 1
    assert [event["step"] for event in events if event["kind"] == "llm.call"] == list(range(1, 11))
    assert [phase["phase"] for phase in record["phases"]] == ["author", "solver", "repair"]
    assert all(phase["offset"] + phase["budget"] <= config.max_steps for phase in record["phases"])
    assert record["check_status"] == "passed"
    assert json.loads((out / "acceptance.json").read_text())["accepted"]["sha256"] == observations[0]["sha256"]
    assert "Observed acceptance result (failed)" in llm.requests[7][0][1]["content"]
    for request_index in (4, 7):
        system = llm.requests[request_index][0][0]["content"]
        assert "No final verification commands are scheduled" not in system
        assert record["accepted"]["command"] in system


def test_pass_on_original_does_not_register_and_solver_still_runs(tmp_path: Path) -> None:
    author, original, solver = (workspace(tmp_path / name) for name in ("author", "original", "solver"))
    out = tmp_path / "out"
    out.mkdir()
    llm = FakeLLM(
        [
            reply(None, call("submit_acceptance_check", requirement="weak check", setup="", assertions=["True"])),
            reply(None, call("finish", summary="could not expose defect")),
            reply(None, call("finish", summary="blocked")),
            reply(None, call("read_file", path="a.py")),
            reply(None, call("edit_file", path="a.py", old="x", new="y")),
            reply(None, call("finish", summary="fixed")),
        ]
    )
    config = settings(tmp_path)
    config.max_steps = 20
    result, record = run_acceptance_experiment(
        llm,
        solver,
        author_workspace=author,
        baseline_workspace=original,
        task="fix a.py",
        output_dir=out,
        settings=config,
        protected=(),
        on_event=lambda *args, **kwargs: None,
    )
    assert result.finished and result.steps == 6
    assert record["check_status"] == "unavailable" and "accepted" not in record
    assert [phase["phase"] for phase in record["phases"]] == ["author", "solver"]
    assert solver.read("a.py") == b"y\n"


def test_all_phases_share_the_cap_and_a_failed_repair_does_not_start_another(tmp_path: Path) -> None:
    author, original, solver = (workspace(tmp_path / name) for name in ("author", "original", "solver"))
    out = tmp_path / "out"
    out.mkdir()
    llm = FakeLLM(
        [
            reply(
                None,
                call(
                    "submit_acceptance_check",
                    requirement="x changes to y",
                    setup="from pathlib import Path\nvalue = Path('a.py').read_text()",
                    assertions=["value == 'y\\n'"],
                ),
            ),
            *(reply(None, call("read_file", path="a.py")) for _ in range(19)),
        ]
    )
    config = settings(tmp_path)
    config.max_steps = 20
    result, record = run_acceptance_experiment(
        llm,
        solver,
        author_workspace=author,
        baseline_workspace=original,
        task="fix a.py",
        output_dir=out,
        settings=config,
        protected=(),
        on_event=lambda *args, **kwargs: None,
    )
    assert not result.finished and result.reason == "budget" and result.steps == 20
    assert len(llm.requests) == 20 and len(record["phases"]) == 3
    assert record["check_status"] == "failed"


def test_author_can_correct_syntax_but_cannot_replace_an_accepted_check(tmp_path: Path) -> None:
    author, original, solver = (workspace(tmp_path / name) for name in ("author", "original", "solver"))
    out = tmp_path / "out"
    out.mkdir()
    spec = dict(
        requirement="x changes to y",
        setup="from pathlib import Path\nvalue = Path('a.py').read_text()",
        assertions=["value == 'y\\n'"],
    )
    llm = FakeLLM(
        [
            reply(None, call("submit_acceptance_check", **(spec | {"setup": "value ="}))),
            reply(None, call("submit_acceptance_check", **spec)),
            reply(None, call("submit_acceptance_check", **(spec | {"assertions": ["False"]}))),
            reply(None, call("finish", summary="accepted")),
            reply(None, call("finish", summary="readonly author done")),
            reply(None, call("read_file", path="a.py")),
            reply(None, call("edit_file", path="a.py", old="x", new="y")),
            reply(None, call("finish", summary="fixed")),
        ]
    )
    config = settings(tmp_path)
    config.max_steps = 20
    result, record = run_acceptance_experiment(
        llm,
        solver,
        author_workspace=author,
        baseline_workspace=original,
        task="fix a.py",
        output_dir=out,
        settings=config,
        protected=(),
        on_event=lambda *args, **kwargs: None,
    )
    assert result.finished and result.steps == 8 and len(record["attempts"]) == 2
    assert record["attempts"][0]["status"] == "error" and "invalid syntax" in record["attempts"][0]["error"]
    assert record["accepted"]["spec"]["assertions"] == ("value == 'y\\n'",)
    assert record["check_status"] == "passed" and "repair_check" not in record
