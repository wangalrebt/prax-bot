"""Workspace creation retries transient sandbox API failures and gives up on everything else."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from contree_sdk.sdk.exceptions import ApiStatusCodeError, ContreeTransportError, ForbiddenError

import prax_bot.sandbox as sandbox
from prax_bot.sandbox import create_workspace, transient
from tests.test_sandbox import FakeImage, base, make_repo, settings


def test_transient_classifies_gateway_failures() -> None:
    assert transient(ContreeTransportError()) and transient(ApiStatusCodeError(status=504))
    assert not transient(ApiStatusCodeError(status=400)) and not transient(ForbiddenError())


def flaky_client(log: list, failures: list[Exception]):
    """A client whose first bootstrap runs raise the given errors before one succeeds."""
    def oci(ref, timeout=None):
        img = base(log)
        real_run = img.run

        def run(**kw):
            if failures:
                raise failures.pop(0)
            return real_run(**kw)

        img.run = run
        return img

    return SimpleNamespace(images=SimpleNamespace(oci=oci, use=lambda ref: FakeImage(ref, FakeImage.registry[ref], log)))


def test_create_workspace_retries_transient_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox.time, "sleep", lambda s: None)
    log: list = []
    events: list[tuple[str, dict]] = []
    client = flaky_client(log, [ApiStatusCodeError(status=504), ContreeTransportError()])
    ws = create_workspace(client, settings(tmp_path), make_repo(tmp_path), lambda k, **p: events.append((k, p)))
    assert ws.image_id.startswith("img-")
    retries = [p for k, p in events if k == "sandbox.retry"]
    assert [r["attempt"] for r in retries] == [1, 2] and retries[0]["step"] == "upload" and "504" in retries[0]["error"] or "ApiStatusCodeError" in retries[0]["error"]


def test_create_workspace_does_not_retry_permanent_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox.time, "sleep", lambda s: None)
    repo = make_repo(tmp_path)
    with pytest.raises(ForbiddenError):
        create_workspace(flaky_client([], [ForbiddenError()]), settings(tmp_path), repo)
    with pytest.raises(ContreeTransportError):
        create_workspace(flaky_client([], [ContreeTransportError()] * 3), settings(tmp_path), repo)
