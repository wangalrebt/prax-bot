from __future__ import annotations

import json
from pathlib import Path

import pytest

from prax_bot.core.verification import CheckResult, attributed, run_check
from prax_bot.workspace.local import LocalWorkspace

OLD = "tests/test_old.py::test_old"
NEW = "tests/test_new.py::test_new"


def test_comparison_retains_baseline_evidence_and_legacy_attribution() -> None:
    baseline = CheckResult(
        "tests", "pytest -q", "clean-image", 1, f"FAILED {OLD}\n", 2.5, where="fork", failures=(OLD,)
    )
    changed = CheckResult(
        "tests", "pytest -q", "changed-image", 1, f"FAILED {OLD}\nFAILED {NEW}\n", where="fork", failures=(OLD, NEW)
    )

    compared = attributed(changed, baseline)
    assert compared.baseline is baseline
    assert compared.baseline_exit_code == 1
    assert compared.new_failures == (NEW,) and compared.known_failures == (OLD,)
    assert compared.attribution == f"1 new with this change: {NEW}; 1 also fails without it"

    saved = json.loads(json.dumps(compared.as_dict()))
    assert saved["baseline"] == json.loads(json.dumps(baseline.as_dict()))
    assert saved["baseline"]["command"] == "pytest -q"
    assert saved["baseline"]["image"] == "clean-image"
    assert saved["baseline"]["where"] == "fork" and saved["baseline"]["passed"] is False
    assert saved["baseline"]["output"] == f"FAILED {OLD}\n"
    assert saved["baseline_exit_code"] == 1 and saved["new_failures"] == [NEW]


@pytest.mark.parametrize(
    ("exit_code", "timed_out", "error"),
    [(None, False, "WorkspaceError: cannot create fork"), (124, True, None), (None, False, None)],
)
def test_incomplete_baseline_is_visible_without_claiming_failure_attribution(
    exit_code: int | None, timed_out: bool, error: str | None
) -> None:
    baseline = CheckResult(
        "tests", "pytest -q", "clean-image", exit_code, "partial output", 4.0, timed_out, error, "fork", (OLD,)
    )
    changed = CheckResult("tests", "pytest -q", "changed-image", 1, "", failures=(OLD, NEW))

    compared = attributed(changed, baseline)
    assert compared.baseline is baseline
    assert compared.baseline_exit_code is None
    assert compared.new_failures == compared.known_failures == () and compared.attribution == ""
    saved = compared.as_dict()["baseline"]
    assert saved["exit_code"] == exit_code and saved["timed_out"] is timed_out and saved["error"] == error
    assert saved["output"] == "partial output" and saved["passed"] is False


def test_passing_comparison_keeps_failures_as_evidence_without_inventing_fixed_tests() -> None:
    baseline = CheckResult("tests", "pytest -q", "clean-image", 1, f"FAILED {OLD}\n", failures=(OLD,))
    changed = CheckResult("tests", "pytest -q", "changed-image", 0, "1 passed")
    compared = attributed(changed, baseline)
    assert compared.passed and compared.attribution == ""
    assert compared.new_failures == compared.known_failures == ()
    assert compared.as_dict()["baseline"]["failures"] == (OLD,)
    assert "fixed" not in compared.as_dict()

    missing = attributed(changed, None)
    assert missing is changed and missing.as_dict()["baseline"] is None


def test_baseline_records_the_clean_image_actually_tested(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "current").mkdir(parents=True)
    workspace = LocalWorkspace(root)
    workspace.mark_clean()
    clean_image = workspace.clean
    workspace.write("changed.py", b"changed\n")

    baseline = run_check(workspace, "tests", "test ! -f changed.py", 5, patch="")
    assert baseline.passed and baseline.image == clean_image and baseline.where == "fork"
    current = run_check(workspace, "tests", "test -f changed.py", 5)
    assert current.passed and current.image == workspace.image_id and current.where == "workspace"
