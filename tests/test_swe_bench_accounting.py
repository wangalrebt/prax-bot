"""Saved agent usage survives grading, including submissions with no patch.

These tests use no benchmark dependencies, dataset, model API, or sandbox.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import swe_bench

ROW = {"instance_id": "owner__repo-1", "repo": "owner/repo", "difficulty": "<15 min fix", "image": "test"}


@pytest.mark.parametrize("patch", [None, "", " \n"])
def test_reeval_without_patch_keeps_usage_and_exit_status(tmp_path: Path, monkeypatch, patch) -> None:
    out = tmp_path / ROW["instance_id"]
    out.mkdir()
    saved = {
        "steps": 50,
        "tokens": 552511,
        "exit_status": "LimitsExceeded",
        "usage_by_model": {"model": {"calls": 50, "prompt_tokens": 550000, "completion_tokens": 2511}},
        "sandbox_commands": 12,
        "sandbox_cost": 0.25,
        "finished": False,
        "reason": "max_steps",
        "resolved": True,
        "patch_applied": True,
        "eval": "resolved",
        "eval_cost": 1.5,
        "grading_attempts": 1,
    }
    metadata = out / "result.json"
    metadata.write_text(json.dumps(saved))
    original = metadata.read_bytes()
    if patch is not None:
        (out / "patch.diff").write_text(patch)
    monkeypatch.setattr(swe_bench, "make_client", lambda: pytest.fail("empty patches must not start a sandbox"))

    result = swe_bench.reeval_instance(ROW, None, tmp_path)

    for key in (
        "steps",
        "tokens",
        "exit_status",
        "usage_by_model",
        "sandbox_commands",
        "sandbox_cost",
        "finished",
        "reason",
    ):
        assert result[key] == saved[key]
    assert result["resolved"] is False and result["patch_applied"] is False
    assert result["eval"] == "no saved patch"
    assert "grading_attempts" not in result and "eval_cost" not in result
    assert metadata.read_bytes() == original


@pytest.mark.parametrize("saved", [None, {}, {"steps": 0, "tokens": 0, "exit_status": "NotStarted"}])
def test_reeval_distinguishes_unknown_usage_from_zero(tmp_path: Path, monkeypatch, saved) -> None:
    out = tmp_path / ROW["instance_id"]
    out.mkdir()
    if saved is not None:
        (out / "result.json").write_text(json.dumps(saved))
    monkeypatch.setattr(swe_bench, "make_client", lambda: pytest.fail("missing patches must not start a sandbox"))

    result = swe_bench.reeval_instance(ROW, None, tmp_path)

    if saved:
        assert result["steps"] == 0 and result["tokens"] == 0
        assert result["exit_status"] == "NotStarted"
    else:
        assert "steps" not in result and "tokens" not in result and "exit_status" not in result
    monkeypatch.setattr(swe_bench.Settings, "from_env", lambda: SimpleNamespace(model="test-model"))
    table = swe_bench.markdown([result], SimpleNamespace())
    assert ("| 0 | 0 | no saved patch |" if saved else "| - | - | no saved patch |") in table


def test_reeval_with_patch_keeps_agent_metadata_and_uses_new_grade(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / ROW["instance_id"]
    out.mkdir()
    patch = "saved patch\n"
    (out / "patch.diff").write_text(patch)
    (out / "result.json").write_text(
        json.dumps({"steps": 7, "tokens": 1234, "exit_status": "Submitted", "resolved": False, "eval": "old error"})
    )
    image = object()
    monkeypatch.setattr(
        swe_bench, "make_client", lambda: SimpleNamespace(images=SimpleNamespace(oci=lambda *a, **k: image))
    )
    monkeypatch.setattr(swe_bench.Settings, "from_env", lambda: SimpleNamespace(import_timeout=10))
    calls = []

    def grade(actual_image, row, diff, scratch):
        calls.append((actual_image, row, diff, scratch))
        return {"resolved": True, "patch_applied": True, "eval": "resolved", "grading_attempts": 1}

    monkeypatch.setattr(swe_bench, "grade", grade)
    result = swe_bench.reeval_instance(ROW, None, tmp_path)

    assert calls == [(image, ROW, patch, out)]
    assert (result["steps"], result["tokens"], result["exit_status"]) == (7, 1234, "Submitted")
    assert result["resolved"] is True and result["eval"] == "resolved" and result["grading_attempts"] == 1
