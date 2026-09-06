from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"
DEFAULT_LLM_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
DEFAULT_BASE_IMAGE = "python:3.12"
WORKDIR = "/work"


def contree_auth_ini() -> Path:
    """Path of the ConTree SDK's optional auth.ini, mirroring the SDK's own lookup."""
    if home := os.environ.get("CONTREE_HOME"):
        return Path(home) / "auth.ini"
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "contree" / "auth.ini"


def read_auth_ini() -> dict[str, str]:
    """Token and project from the SDK's auth.ini active profile, if the file exists.

    The same file authenticates the sandbox SDK, so one file can serve both the model
    endpoint and the sandbox. The profile is chosen the way the SDK chooses it.
    """
    path = contree_auth_ini()
    if not path.is_file():
        return {}
    import configparser

    cp = configparser.ConfigParser()
    try:
        cp.read(path)
    except (OSError, configparser.Error):
        return {}
    active = os.environ.get("CONTREE_PROFILE") or cp.defaults().get("profile", "default")
    section = f"profile:{active}"
    if not cp.has_section(section):
        return {}
    return {k: v for k, v in cp[section].items() if v}


@dataclass
class Settings:
    api_key: str | None
    project_id: str | None
    model: str
    llm_base_url: str
    base_image: str
    setup: str | None
    max_steps: int
    command_timeout: int
    setup_timeout: int
    import_timeout: int
    home: Path
    investigate_model: str | None = None
    investigate_steps: int = 6

    @classmethod
    def from_env(
        cls,
        *,
        model: str | None = None,
        investigate_model: str | None = None,
        base_image: str | None = None,
        setup: str | None = None,
        max_steps: int | None = None,
    ) -> Settings:
        env = os.environ
        ini = read_auth_ini()
        return cls(
            api_key=env.get("NEBIUS_API_KEY") or env.get("CONTREE_TOKEN") or ini.get("token") or None,
            project_id=(
                env.get("NEBIUS_PROJECT_ID")
                or env.get("NEBIUS_AI_PROJECT")
                or env.get("CONTREE_PROJECT")
                or ini.get("project")
                or None
            ),
            model=model or env.get("PRAXBOT_MODEL") or DEFAULT_MODEL,
            investigate_model=investigate_model or env.get("PRAXBOT_INVESTIGATE_MODEL") or None,
            investigate_steps=int(env.get("PRAXBOT_INVESTIGATE_STEPS", "6")),
            llm_base_url=env.get("PRAXBOT_LLM_BASE_URL") or DEFAULT_LLM_BASE_URL,
            base_image=base_image or env.get("PRAXBOT_BASE_IMAGE") or DEFAULT_BASE_IMAGE,
            setup=setup or env.get("PRAXBOT_SETUP") or None,
            max_steps=max_steps or int(env.get("PRAXBOT_MAX_STEPS", "40")),
            command_timeout=int(env.get("PRAXBOT_COMMAND_TIMEOUT", "300")),
            setup_timeout=int(env.get("PRAXBOT_SETUP_TIMEOUT", "900")),
            import_timeout=int(env.get("PRAXBOT_IMPORT_TIMEOUT", "900")),
            home=Path(env.get("PRAXBOT_HOME") or Path.home() / ".praxbot"),
        )

    def missing(self) -> list[str]:
        """Human-readable list of credentials that are not configured."""
        problems: list[str] = []
        hint = f" Either export it or put it in {contree_auth_ini()} under [profile:default]."
        if not self.api_key:
            problems.append("NEBIUS_API_KEY is not set (Token Factory API key; also authenticates Sandboxes)." + hint)
        if not self.project_id:
            problems.append("NEBIUS_PROJECT_ID is not set (Nebius project id, sent to Sandboxes as the Project header)." + hint)
        return problems
