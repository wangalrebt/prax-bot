"""Goal runs: the agent works in rounds on the same workspace until the objective is met.

A round is one agent transcript. After each round the driver decides what happens next: stop because the
check command passes or the model reported the goal met, stop because the round cap is reached or two rounds
in a row changed nothing, or start another round with the check output as the instruction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CHECK_OUTPUT_TAIL = 3000
STALL_ROUNDS = 2  # rounds in a row with an unchanged diff before the goal is declared blocked


def goal_task(objective: str, max_rounds: int, check: str | None) -> str:
    """The first round's task text."""
    how = (
        f"The goal is met when this command exits 0 in the repository: {check}"
        if check
        else "You decide when the goal is met; verify it before saying so."
    )
    return (
        f"Goal: {objective.strip()}\n\n"
        f"This is a goal run of up to {max_rounds} rounds; each round is a fresh conversation on the same workspace, "
        f"so leave notes for yourself in the code, not in prose. {how}\n"
        "When the whole goal is achieved and verified, call finish with goal_met=true. "
        "If it is not, call finish with goal_met=false and say exactly what remains."
    )


@dataclass
class GoalDriver:
    """Decides after each round whether to continue, and with what instruction."""

    objective: str
    workspace: Any
    max_rounds: int = 5
    check: str | None = None
    check_timeout: int = 300
    rounds: int = 0
    met: bool = False
    stopped: str = ""  # why the goal run ended, other than being met
    check_output: str = ""
    _last_diffs: list[str] = field(default_factory=list)

    def run_check(self) -> tuple[bool, str]:
        assert self.check is not None
        r = self.workspace.run(self.check, timeout=self.check_timeout, mutate=False)
        out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        if r.timed_out:
            out += f"\n(timed out after {self.check_timeout}s)"
        return r.exit_code == 0 and not r.timed_out, out[-CHECK_OUTPUT_TAIL:]

    def next_instruction(self, round_no: int, result: Any, diff: str) -> str | None:
        """None ends the goal run; a string starts the next round with that instruction."""
        self.rounds = round_no
        if self.check:
            passed, self.check_output = self.run_check()
            if passed:
                self.met = True
                return None
        elif result.goal_met:
            self.met = True
            return None
        if round_no >= self.max_rounds:
            self.stopped = f"round cap of {self.max_rounds} reached"
            return None
        self._last_diffs = [*self._last_diffs, diff][-STALL_ROUNDS:]
        if len(self._last_diffs) == STALL_ROUNDS and len(set(self._last_diffs)) == 1:
            self.stopped = f"no change to the repository in {STALL_ROUNDS} rounds in a row"
            return None
        if result.reason == "error":
            self.stopped = "the agent stopped with an error"
            return None
        why = (
            f"The check command still fails. Command: {self.check}\nOutput (tail):\n{self.check_output}"
            if self.check
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
