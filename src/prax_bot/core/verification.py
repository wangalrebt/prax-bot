"""Explicit check results, tied to the workspace image they speak for.

With a clean image a check runs in a disposable fork of it that holds exactly the diff an apply would write, so a
pass means the user's tree would pass. The same command run in a fork without the diff is the baseline, and it says
which failures are the change's and which were there before.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Any, Literal

from prax_bot.workspace.types import Workspace

VerificationStatus = Literal["not configured", "not run", "incomplete", "stale", "passed", "failed"]
PYTEST_FAILED_RE = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?: - .*)?$", re.MULTILINE)
SHOWN_FAILURES = 5


def pytest_failures(output: str) -> list[str]:
    """Test ids reported as FAILED or ERROR in pytest output, in order."""
    return list(dict.fromkeys(m.group(1) for m in PYTEST_FAILED_RE.finditer(output)))


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
    where: str = "workspace"  # "fork": a disposable fork of the clean image holding exactly the diff an apply writes
    failures: tuple[str, ...] = ()  # failing test ids read from the whole output, before it was shortened
    baseline_exit_code: int | None = None  # the same command in a fork of the clean image without the diff
    new_failures: tuple[str, ...] = ()  # failing tests that do not fail without the diff
    known_failures: tuple[str, ...] = ()  # failing tests that fail without the diff too
    baseline: CheckResult | None = None  # complete evidence, including a baseline that failed to finish

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None

    @property
    def attribution(self) -> str:
        """What the baseline run says about a failure, in one line; empty when there is nothing to say."""
        if self.passed or self.baseline_exit_code is None:
            return ""
        if self.new_failures or self.known_failures:
            parts = []
            if self.new_failures:
                shown = ", ".join(self.new_failures[:SHOWN_FAILURES])
                more = " …" if len(self.new_failures) > SHOWN_FAILURES else ""
                parts.append(f"{len(self.new_failures)} new with this change: {shown}{more}")
            if self.known_failures:
                known = len(self.known_failures)
                parts.append(f"{known} also fail{'s' if known == 1 else ''} without it")
            return "; ".join(parts)
        return "also fails without this change" if self.baseline_exit_code != 0 else "passes without this change"

    def as_dict(self) -> dict[str, Any]:
        return {
            **dataclasses.asdict(self),
            "passed": self.passed,
            "attribution": self.attribution,
            "baseline": self.baseline.as_dict() if self.baseline is not None else None,
        }


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


def run_check(workspace: Workspace, name: str, command: str, timeout: int, *, patch: str | None = None) -> CheckResult:
    """Run a configured command disposably; failures stay visible as check results.

    With `patch` given and a clean image known, the command runs in a fork of the clean image with the patch applied
    (an empty patch is the baseline itself). Otherwise it runs on the workspace as it stands.
    """
    image = workspace.image_id
    clean = getattr(workspace, "clean", None)
    forked = patch is not None and clean is not None
    if patch is not None and not patch.strip() and clean is not None:
        image = clean
    where = "fork" if forked else "workspace"
    try:
        if forked:
            result = workspace.run_clean(command, patch=patch or "", timeout=timeout)
        else:
            result = workspace.run(command, timeout=timeout, mutate=False)
    except Exception as error:
        return CheckResult(
            name, command, image, None, "", error=f"{type(error).__name__}: {str(error)[:500]}", where=where
        )
    output = result.stdout + ("\n" + result.stderr if result.stderr else "")
    return CheckResult(
        name,
        command,
        image,
        result.exit_code,
        output[-6000:],
        result.elapsed_s,
        result.timed_out,
        where=where,
        failures=tuple(pytest_failures(output)),
    )


def attributed(check: CheckResult, baseline: CheckResult | None) -> CheckResult:
    """The check with what the baseline run knows: which of its failing tests fail without the diff too."""
    if baseline is None:
        return check
    if baseline.error is not None or baseline.exit_code is None or baseline.timed_out:
        return dataclasses.replace(
            check, baseline=baseline, baseline_exit_code=None, new_failures=(), known_failures=()
        )
    before = set(baseline.failures)
    return dataclasses.replace(
        check,
        baseline=baseline,
        baseline_exit_code=baseline.exit_code,
        new_failures=tuple(name for name in check.failures if name not in before),
        known_failures=tuple(name for name in check.failures if name in before),
    )
