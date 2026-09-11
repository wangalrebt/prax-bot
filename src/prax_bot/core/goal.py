"""Goal runs: the agent works in rounds on the same workspace until the objective is met.

A round is one agent transcript. After each round the driver decides what happens next: stop because the
check command passes or the model reported the goal met, stop because the round cap is reached or two rounds
in a row changed nothing, or start another round with the check output as the instruction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from prax_bot.core.prompts import prompt
from prax_bot.core.verification import VerificationStatus

if TYPE_CHECKING:
    from prax_bot.core.agent import AgentResult

CHECK_OUTPUT_TAIL = 3000
STALL_ROUNDS = 2  # rounds in a row with an unchanged diff before the goal is declared blocked


def goal_task(objective: str, max_rounds: int, check: str | None) -> str:
    """The first round's task text."""
    how = prompt("goal_how_check", check=check) if check else prompt("goal_how_model")
    return prompt("goal", objective=objective.strip(), max_rounds=max_rounds, how=how)


@dataclass
class GoalDriver:
    """Decide whether to continue using the session's verification evidence."""

    objective: str
    max_rounds: int = 5
    check: str | None = None
    rounds: int = 0
    met: bool = False
    stopped: str = ""  # why the goal run ended, other than being met
    check_output: str = ""
    _last_diffs: list[str] = field(default_factory=list)

    def next_instruction(
        self,
        round_no: int,
        result: AgentResult,
        diff: str,
        *,
        verification: VerificationStatus,
        check_output: str = "",
    ) -> str | None:
        """None ends the goal run; a string starts the next round with that instruction."""
        self.rounds = round_no
        self.check_output = check_output[-CHECK_OUTPUT_TAIL:]
        if result.reason == "cancelled":
            self.stopped = "cancelled by the user"
            return None
        if result.reason == "error":
            self.stopped = "the agent stopped with an error"
            return None
        verified = verification == "passed"
        if (self.check and verified) or (
            not self.check and result.goal_met and verification in ("passed", "not configured")
        ):
            self.met = True
            return None
        if round_no >= self.max_rounds:
            self.stopped = f"round cap of {self.max_rounds} reached"
            return None
        self._last_diffs = [*self._last_diffs, diff][-STALL_ROUNDS:]
        if len(self._last_diffs) == STALL_ROUNDS and len(set(self._last_diffs)) == 1:
            self.stopped = f"no change to the repository in {STALL_ROUNDS} rounds in a row"
            return None
        why = (
            f"The configured verification commands have not all passed ({verification})."
            + (f"\nCommand: {self.check}\nOutput (tail):\n{self.check_output}" if self.check else "")
            if verification not in ("passed", "not configured")
            else "You reported the goal is not met yet."
        )
        return (
            f"Round {round_no + 1} of {self.max_rounds}. {why}\n"
            "Continue toward the goal. When it is fully achieved and verified, call finish with goal_met=true."
        )

    @property
    def status(self) -> str:
        if self.met:
            return f"goal met after {self.rounds} round{'s' if self.rounds != 1 else ''}"
        return f"goal not met: {self.stopped or 'stopped'} after {self.rounds} round{'s' if self.rounds != 1 else ''}"
