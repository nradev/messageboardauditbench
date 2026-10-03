"""Output-token budget for native ReAct trials.

A wall-clock budget mostly measures provider latency and throughput, which vary
far more across OpenRouter providers than across models. This budget instead
counts what the model generates: cumulative output tokens for the sample,
reasoning included (OpenAI-compatible ``completion_tokens``).

The budget is soft at the turn boundary. The turn that crosses it finishes,
including its tool calls; the agent is then told the budget is spent and gets
exactly one final turn to finish report.md, after which the loop stops. Actual
usage therefore lands slightly above the budget, and both are recorded.
"""

from __future__ import annotations

import math

from inspect_ai.agent import AgentState
from inspect_ai.model._model import sample_model_usage

FINAL_TURN_MESSAGE = (
    "Your output-token budget is spent. This is your final turn: make any last "
    "edits that report.md needs now, in this turn. The session stops after it. "
    "Keep report.md in place: edit it, never delete, move, or truncate it. If "
    "report.md is missing at the end the trial scores zero."
)


def sample_output_tokens() -> int:
    """Output tokens generated so far in this sample, across every model."""
    return sum(usage.output_tokens for usage in sample_model_usage().values())


class OutputTokenBudget:
    """Track one sample's output tokens and end the ReAct loop after the final turn."""

    def __init__(self, budget: int, min_fraction: float = 0.0) -> None:
        if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
            raise ValueError("token budget must be a positive integer")
        if not 0 <= min_fraction < 1:
            raise ValueError("min_fraction must be between 0 (inclusive) and 1")
        self.budget = budget
        self.minimum = math.ceil(budget * min_fraction)
        self._baseline = sample_output_tokens()
        self.used_at_exhaustion: int | None = None
        self.final_turn_sent = False
        self.finished = False

    @property
    def used(self) -> int:
        return sample_output_tokens() - self._baseline

    @property
    def left(self) -> int:
        return max(0, self.budget - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used_at_exhaustion is not None or self.used >= self.budget

    @property
    def minimum_reached(self) -> bool:
        return self.used >= self.minimum

    async def on_continue(self, state: AgentState) -> bool | str:
        """``react(on_continue=...)``: called after each turn and its tool calls."""
        if self.final_turn_sent:
            self.finished = True
            return False
        if self.used >= self.budget:
            self.used_at_exhaustion = self.used
            self.final_turn_sent = True
            return FINAL_TURN_MESSAGE
        # React's default: continue, prompting the model only if it made no tool call.
        return True

    def note(self) -> str:
        """The budget line appended to every ReAct tool result."""
        if self.final_turn_sent:
            return "Token budget: spent. This is your final turn; finish report.md now."
        if self.used >= self.budget:
            # Tool results of the turn that crossed the budget, before the final turn.
            return (
                f"Token budget: spent ({self.used:,} of {self.budget:,} output tokens "
                "used). You get one final turn next to finish report.md."
            )
        text = (
            f"Token budget: about {self.left:,} of {self.budget:,} output tokens left "
            f"({self.used:,} used). When it runs out you get one final turn to finish "
            "report.md."
        )
        if not self.minimum_reached:
            text += (
                " Minimum-budget policy: keep doing meaningful work until at least "
                f"{self.minimum:,} output tokens are used; do not idle."
            )
        return text

    def metadata(self) -> dict[str, int | bool | None]:
        used = self.used
        return {
            "budget_tokens": self.budget,
            "budget_tokens_minimum": self.minimum,
            "budget_tokens_used": used,
            "budget_tokens_used_at_exhaustion": self.used_at_exhaustion,
            "budget_tokens_overrun": max(0, used - self.budget),
            "budget_tokens_exhausted": self.used_at_exhaustion is not None,
            "budget_tokens_final_turn": self.final_turn_sent,
        }
