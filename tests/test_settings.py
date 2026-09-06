from __future__ import annotations

from pathlib import Path

import pytest

from prax_bot.settings import Settings, read_auth_ini


def test_settings_fall_back_to_the_sdk_auth_ini(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.delenv("NEBIUS_PROJECT_ID", raising=False)
    monkeypatch.setenv("CONTREE_HOME", str(tmp_path))
    assert Settings.from_env().missing() and read_auth_ini() == {}

    (tmp_path / "auth.ini").write_text("[profile:default]\ntoken = key-123\nproject = project-abc\n")
    s = Settings.from_env()
    assert (s.api_key, s.project_id) == ("key-123", "project-abc") and s.missing() == []

    monkeypatch.setenv("NEBIUS_API_KEY", "env-key")
    assert Settings.from_env().api_key == "env-key"

    monkeypatch.setenv("CONTREE_PROFILE", "other")
    assert read_auth_ini() == {}

    monkeypatch.setenv("NEBIUS_AI_PROJECT", "cli-project")
    assert Settings.from_env().project_id == "cli-project"
