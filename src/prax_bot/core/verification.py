"""Explicit check results, tied to the workspace image on which they ran."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from prax_bot.workspace.types import Workspace

VerificationStatus = Literal["not configured", "not run", "incomplete", "stale", "passed", "failed"]


@dataclass(frozen=True)
class CheckResult:
    name: str
    command: str
    image: str
    exit_code: int | None
    output: str
    elapsed_s: float = 0.0
    timed_out: bool = False
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


def verification_status(
    expected: tuple[tuple[str, str], ...], checks: list[CheckResult], image: str
) -> VerificationStatus:
    """Evaluate evidence for every required command on the current workspace image."""
    if not expected:
        return "not configured"
    if not checks:
        return "not run"
    relevant = [check for check in checks if (check.name, check.command) in expected]
    if {(check.name, check.command) for check in relevant} != set(expected):
        return "incomplete"
    if any(check.image != image for check in relevant):
        return "stale"
    return "passed" if all(check.passed for check in relevant) else "failed"


def run_check(workspace: Workspace, name: str, command: str, timeout: int) -> CheckResult:
    """Run a configured command disposably; failures stay visible as check results."""
    image = workspace.image_id
    try:
        result = workspace.run(command, timeout=timeout, mutate=False)
    except Exception as error:
        return CheckResult(name, command, image, None, "", error=f"{type(error).__name__}: {str(error)[:500]}")
    output = result.stdout + ("\n" + result.stderr if result.stderr else "")
    return CheckResult(name, command, image, result.exit_code, output[-6000:], result.elapsed_s, result.timed_out)
