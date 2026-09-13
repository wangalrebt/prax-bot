"""Configuration, in one dataclass, from flags, environment variables, config files and the sandbox auth file.

Precedence, highest first: a command-line flag, an environment variable (PRAXBOT_*), the repository's
.praxbot/config.toml, the user's ~/.praxbot/config.toml, the built-in default. Every value remembers where
it came from, which `prax config` shows. A repository's file cannot choose the backend or the model
endpoint, and setup or verification commands it provides only run inside a sandbox.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"
DEFAULT_LLM_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
EFFORT_LEVELS = ("off", "low", "high", "xhigh")  # how much the model reasons per step; high is its default
DEFAULT_BASE_IMAGE = "python:3.12"
WORKDIR = "/work"
BACKENDS = ("contree", "local")
CONFIG_FILE = "config.toml"  # under the home directory
REPO_CONFIG_FILE = ".praxbot/config.toml"  # under the repository root
# keys a config file may set; a repository's file gets the subset that cannot redirect or endanger the machine
USER_KEYS: dict[str, type] = {
    "model": str,
    "investigate_model": str,
    "investigate_steps": int,
    "base_image": str,
    "setup": str,
    "max_steps": int,
    "effort": str,
    "temperature": float,
    "top_p": float,
    "backend": str,
    "llm_base_url": str,
    "command_timeout": int,
    "setup_timeout": int,
    "import_timeout": int,
    "context_budget": int,
    "system_prompt_file": str,
    "test_command": str,
    "lint_command": str,
}
TYPE_WORDS = {str: "a string", int: "an integer", float: "a number"}
REPO_KEYS = (
    "model",
    "investigate_model",
    "base_image",
    "setup",
    "max_steps",
    "effort",
    "temperature",
    "top_p",
    "context_budget",
    "test_command",
    "lint_command",
)


def read_prompt_file(path_text: str) -> str:
    """The text of a system prompt file, with a clear error when it cannot be used."""
    path = Path(path_text).expanduser()
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ValueError(f"system_prompt_file {path} could not be read: {e}") from None
    if not text:
        raise ValueError(f"system_prompt_file {path} is empty")
    return text


def load_config(home: Path, repo_root: Path | None) -> tuple[dict[str, Any], dict[str, str], list[str], list[Path]]:
    """Values from the user's and the repository's config files, with where each came from and any complaints.

    Returns (values, sources, warnings, files_read). The repository's file wins over the user's for the
    keys it is allowed to set; keys it is not allowed to set are reported and ignored, never applied.
    """
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    warnings: list[str] = []
    files: list[Path] = []
    candidates = [(home / CONFIG_FILE, "user config", set(USER_KEYS))]
    if repo_root is not None:
        candidates.append((repo_root / REPO_CONFIG_FILE, "repo config", set(REPO_KEYS)))
    for path, label, allowed in candidates:
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ValueError(f"{path} could not be read: {e}") from None
        files.append(path)
        for key, value in data.items():
            if key not in USER_KEYS:
                warnings.append(f"{path}: unknown setting {key!r} ignored")
                continue
            if key not in allowed:
                warnings.append(f"{path}: {key!r} may not be set by a repository's config and was ignored")
                continue
            expected = USER_KEYS[key]
            coerced = (
                float(value) if expected is float and isinstance(value, int) and not isinstance(value, bool) else value
            )
            if not isinstance(coerced, expected) or isinstance(coerced, bool):
                raise TypeError(f"{path}: {key} must be {TYPE_WORDS[expected]}, not {type(value).__name__}")
            values[key] = coerced
            sources[key] = label
    return values, sources, warnings, files


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
    temperature: float = 1.0
    top_p: float | None = 0.95
    effort: str = "low"  # off: no reasoning; low: short reasoning (default, measured); high: the model's default; xhigh: default reasoning with a 32k completion cap
    backend: str = "contree"
    context_budget: int = 80_000  # tokens of context per model call before the transcript is summarised
    system_prompt_file: str | None = None  # a file whose text replaces the built-in system prompt; user-level only
    test_command: str | None = None
    lint_command: str | None = None
    sources: dict[str, str] = field(default_factory=dict)  # setting -> flag, env, repo config, user config, default
    warnings: list[str] = field(default_factory=list)  # things a config file got wrong, worth one line each
    config_files: list[Path] = field(default_factory=list)

    @classmethod
    def from_env(
        cls,
        *,
        model: str | None = None,
        investigate_model: str | None = None,
        base_image: str | None = None,
        setup: str | None = None,
        max_steps: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        effort: str | None = None,
        backend: str | None = None,
        context_budget: int | None = None,
        system_prompt_file: str | None = None,
        test_command: str | None = None,
        lint_command: str | None = None,
        repo_root: Path | None = None,
    ) -> Settings:
        env = os.environ
        ini = read_auth_ini()
        home = Path(env.get("PRAXBOT_HOME") or Path.home() / ".praxbot")
        cfg, sources, warnings, files = load_config(home, repo_root)
        flags = {
            "model": model,
            "investigate_model": investigate_model,
            "base_image": base_image,
            "setup": setup,
            "max_steps": max_steps,
            "temperature": temperature,
            "top_p": top_p,
            "effort": effort,
            "backend": backend,
            "context_budget": context_budget,
            "system_prompt_file": system_prompt_file,
            "test_command": test_command,
            "lint_command": lint_command,
        }

        def pick(key: str, default: Any, cast: Any = str) -> Any:
            """The value for one setting and where it came from, by precedence."""
            if flags.get(key) is not None:
                sources[key] = "flag"
                return flags[key]
            raw = env.get(f"PRAXBOT_{key.upper()}")
            if raw not in (None, ""):
                sources[key] = "env"
                try:
                    return cast(raw)
                except ValueError:
                    raise ValueError(f"PRAXBOT_{key.upper()} must be a {cast.__name__}, not {raw!r}") from None
            if key in cfg:
                return cfg[key]  # its source was recorded by load_config
            sources[key] = "default"
            return default

        effort = str(pick("effort", "low")).lower()
        if effort not in EFFORT_LEVELS:
            raise ValueError(f"effort must be one of {', '.join(EFFORT_LEVELS)}, not {effort!r}")
        backend = str(pick("backend", "contree")).lower()
        if backend not in BACKENDS:
            raise ValueError(f"backend must be one of {', '.join(BACKENDS)}, not {backend!r}")
        top_p_value = pick("top_p", 0.95, float)
        prompt_file = pick("system_prompt_file", None)
        if prompt_file:
            read_prompt_file(prompt_file)  # fail now, with the file named, not in the middle of a run
        # commands a repository's config provides run only inside a sandbox, never on this machine
        commands: dict[str, str | None] = {}
        for key in ("setup", "test_command", "lint_command"):
            value = pick(key, None)
            if backend == "local" and sources.get(key) == "repo config":
                warnings.append(
                    f"the repository's config sets {key}, which only runs inside a sandbox; "
                    f"on the local backend pass --{key.replace('_', '-')} yourself to run it on this machine"
                )
                value = None
                sources[key] = "ignored"
            commands[key] = value or None
        return cls(
            temperature=float(pick("temperature", 1.0, float)),
            top_p=None if top_p_value is None else float(top_p_value),
            effort=effort,
            backend=backend,
            api_key=env.get("NEBIUS_API_KEY") or env.get("CONTREE_TOKEN") or ini.get("token") or None,
            project_id=(
                env.get("NEBIUS_PROJECT_ID")
                or env.get("NEBIUS_AI_PROJECT")
                or env.get("CONTREE_PROJECT")
                or ini.get("project")
                or None
            ),
            model=str(pick("model", DEFAULT_MODEL)),
            investigate_model=pick("investigate_model", None),
            investigate_steps=int(pick("investigate_steps", 6, int)),
            llm_base_url=str(pick("llm_base_url", DEFAULT_LLM_BASE_URL)),
            base_image=str(pick("base_image", DEFAULT_BASE_IMAGE)),
            setup=commands["setup"],
            max_steps=int(pick("max_steps", 40, int)),
            command_timeout=int(pick("command_timeout", 300, int)),
            setup_timeout=int(pick("setup_timeout", 900, int)),
            import_timeout=int(pick("import_timeout", 900, int)),
            context_budget=int(pick("context_budget", 80_000, int)),
            system_prompt_file=str(prompt_file) if prompt_file else None,
            test_command=commands["test_command"],
            lint_command=commands["lint_command"],
            home=home,
            sources=sources,
            warnings=warnings,
            config_files=files,
        )

    def system_prompt_text(self) -> str | None:
        """The text of the configured system prompt file, or None for the built-in prompt."""
        return read_prompt_file(self.system_prompt_file) if self.system_prompt_file else None

    def effective(self) -> list[tuple[str, str, str]]:
        """(setting, value, source) for everything a config file can set, for `prax config`."""
        rows = []
        for key in USER_KEYS:
            value = getattr(self, key)
            rows.append((key, "" if value is None else str(value), self.sources.get(key, "default")))
        return rows

    def missing(self) -> list[str]:
        """Human-readable list of credentials that are not configured."""
        problems: list[str] = []
        hint = f" Either export it or put it in {contree_auth_ini()} under [profile:default]."
        if not self.api_key:
            problems.append("NEBIUS_API_KEY is not set (Token Factory API key; also authenticates Sandboxes)." + hint)
        if not self.project_id:
            problems.append(
                "NEBIUS_PROJECT_ID is not set (Nebius project id, sent to Sandboxes as the Project header)." + hint
            )
        return problems
