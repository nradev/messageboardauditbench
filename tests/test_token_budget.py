"""The output-token budget: soft at the turn boundary, then exactly one final turn."""

from __future__ import annotations

import asyncio

import pytest

from messageboard_audit_bench import token_budget
from messageboard_audit_bench.token_budget import FINAL_TURN_MESSAGE, OutputTokenBudget


@pytest.fixture
def usage(monkeypatch):
    counter = {"tokens": 0}
    monkeypatch.setattr(token_budget, "sample_output_tokens", lambda: counter["tokens"])
    return counter


def turn(budget: OutputTokenBudget):
    return asyncio.run(budget.on_continue(None))


def test_crossing_turn_finishes_then_one_final_turn(usage) -> None:
    budget = OutputTokenBudget(1000, min_fraction=0.75)

    usage["tokens"] = 600
    assert turn(budget) is True
    assert not budget.minimum_reached

    usage["tokens"] = 1300  # this turn crosses the budget; its tools already ran
    assert turn(budget) == FINAL_TURN_MESSAGE
    assert budget.exhausted and budget.final_turn_sent and not budget.finished

    usage["tokens"] = 1500  # the final turn
    assert turn(budget) is False
    assert budget.finished

    meta = budget.metadata()
    assert meta["budget_tokens"] == 1000
    assert meta["budget_tokens_minimum"] == 750
    assert meta["budget_tokens_used_at_exhaustion"] == 1300
    assert meta["budget_tokens_used"] == 1500
    assert meta["budget_tokens_overrun"] == 500
    assert meta["budget_tokens_final_turn"] is True


def test_usage_before_the_agent_starts_is_not_charged(usage) -> None:
    usage["tokens"] = 400
    budget = OutputTokenBudget(1000)
    usage["tokens"] = 900
    assert budget.used == 500 and budget.left == 500
    assert turn(budget) is True


def test_note_reports_tokens_left_and_minimum(usage) -> None:
    budget = OutputTokenBudget(25000, min_fraction=0.75)
    usage["tokens"] = 5000
    note = budget.note()
    assert "about 20,000 of 25,000 output tokens left (5,000 used)" in note
    assert "at least 18,750 output tokens" in note
    usage["tokens"] = 26000
    assert "spent (26,000 of 25,000 output tokens used)" in budget.note()
    turn(budget)
    assert "This is your final turn" in budget.note()


def test_rejects_invalid_budgets(usage) -> None:
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            OutputTokenBudget(bad)
    with pytest.raises(ValueError):
        OutputTokenBudget(1000, min_fraction=1)


def test_provider_reported_cost_sums_openrouter_usage() -> None:
    from inspect_ai.event import ModelEvent
    from inspect_ai.model import GenerateConfig, ModelCall, ModelOutput

    from messageboard_audit_bench.native import _provider_reported_cost

    def event(usage):
        return ModelEvent(
            model="openrouter/z-ai/glm-5.3", input=[], tools=[], tool_choice="auto",
            config=GenerateConfig(), output=ModelOutput(),
            call=ModelCall(request={}, response={"usage": usage}),
        )

    events = [event({"cost": 0.25}), event({"cost": 0.125}), event({}), "not an event"]
    assert _provider_reported_cost(events) == 0.375
    assert _provider_reported_cost([event({})]) is None
