"""The Inspect rubric scorer, driven by canned judge replies.

No API calls: `mockllm/model` with `custom_outputs` returns whatever the test hands it,
which is the pattern `tests/test_solver.py` already uses for the legacy scorer.

What matters here is the failure behaviour as much as the arithmetic. A report where every
sheet failed is not a report that scored zero — an auth or billing error takes all of them
down at once — and recording it as zero is both a wrong number and, because the resume path
skips anything already written, a permanently wrong one. So the scorer must come back
unscored and the exporter must write nothing.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from inspect_ai.model import (
    ChatMessageUser,
    ModelName,
    ModelOutput,
    ModelUsage,
    get_model,
)
from inspect_ai.scorer import Target
from inspect_ai.solver import TaskState

from messageboard_audit_bench.grading import core, export
from messageboard_audit_bench.grading.scorer import sheet_scorer

REPORT = "## TL;DR\nAgents from OpenAI colluded on a wiki.\n\n## Findings\nDetail.\n"


def state(report: str = REPORT, sample_id: str = "r4b10_test_rep1") -> TaskState:
    st = TaskState(
        model=ModelName("mockllm/model"),
        sample_id=sample_id,
        epoch=1,
        input=report,
        messages=[ChatMessageUser(content=report)],
        metadata={"title": "a staged report"},
        target=Target(""),
    )
    st.output = ModelOutput.from_content(model="staged-report", content=report)
    return st


def judge(replies: list[str]):
    """A mock judge that returns `replies` in order, then repeats the last one."""
    seen = iter(replies)
    last: list[str] = [replies[-1]]

    def nxt(*_args, **_kwargs) -> ModelOutput:
        try:
            last[0] = next(seen)
        except StopIteration:
            pass
        return ModelOutput(model="mockllm/model", completion=last[0], usage=ModelUsage())

    return get_model("mockllm/model", custom_outputs=nxt)


def judge_by_sheet(mode: str, replies: dict[str, list[str]]):
    """A mock judge that answers each sheet from its own reply list, in order.

    Sheets are graded concurrently, so replies cannot be handed out by call order: each
    prompt is matched to its sheet by the sheet's first claim heading.
    """
    sets, _ = core.load_sheets(mode)
    queues = {spec["rubric_id"]: iter(replies[spec["rubric_id"]]) for spec in sets}
    last = {rid: r[-1] for rid, r in replies.items()}
    markers = {spec["rubric_id"]: f"## {spec['claims'][0]['id']}" for spec in sets}

    def nxt(messages, *_args, **_kwargs) -> ModelOutput:
        text = "\n".join(m.text for m in messages)
        rid = next(r for r, marker in markers.items() if marker in text)
        try:
            last[rid] = next(queues[rid])
        except StopIteration:
            pass
        return ModelOutput(model="mockllm/model", completion=last[rid], usage=ModelUsage())

    return get_model("mockllm/model", custom_outputs=nxt)


def sheet_reply(mode: str, index: int, score: float) -> str:
    """A well-formed reply giving every claim on sheet `index` the same score."""
    sets, _ = core.load_sheets(mode)
    spec = sets[index]
    items = [{"id": c["id"], "score": score, "quote": "", "reason": "x"} for c in spec["claims"]]
    return json.dumps({"rubric_id": spec["rubric_id"], "items": items})


async def test_scores_every_sheet_and_reports_the_mean() -> None:
    sets, _ = core.load_sheets("v2")
    replies = [sheet_reply("v2", i, 1.0) for i in range(len(sets))]
    score = await sheet_scorer(rubric="v2", judge=judge(replies))(state(), Target(""))

    grade = score.metadata["grade"]
    assert grade["max"] == 38, "all 38 points should have been graded"
    assert score.value == 1.0
    assert grade["accuracy"] == 1.0
    assert len(grade["per_rubric"]) == len(sets)
    assert not score.metadata["failures"]


async def test_scores_are_clamped_and_rounded() -> None:
    sets, _ = core.load_sheets("v2")
    # 1.44 rounds to 1.4 then clamps to 1.0; -3 clamps to 0.0
    replies = [sheet_reply("v2", 0, 1.44)] + [sheet_reply("v2", i, -3) for i in range(1, len(sets))]
    score = await sheet_scorer(rubric="v2", judge=judge(replies))(state(), Target(""))

    values = {i["score"] for i in score.metadata["grade"]["scores"].values()}
    assert values <= {0.0, 1.0}
    assert 1.0 in values and 0.0 in values


async def test_prose_wrapped_json_is_recovered() -> None:
    body = sheet_reply("tldrh", 0, 0.7)
    wrapped = f"Here is my assessment.\n```json\n{body}\n```\nHope that helps."
    score = await sheet_scorer(rubric="tldrh", judge=judge([wrapped]))(state(), Target(""))

    assert score.value == 0.7
    assert not score.metadata["failures"]


async def test_unparseable_reply_is_retried_then_recorded_as_a_sheet_failure() -> None:
    score = await sheet_scorer(rubric="tldrh", judge=judge(["not json at all"]))(
        state(), Target("")
    )

    # tldrh is a single sheet, so its failure is a total failure. Score.unscored carries
    # NaN, which is how Inspect keeps it in the log but out of the metrics.
    assert math.isnan(score.value), "an ungraded report must not be scored"
    assert "TLDRH" in score.metadata["failures"]


async def test_one_bad_sheet_does_not_discard_the_others() -> None:
    sets, _ = core.load_sheets("v2")
    replies = {spec["rubric_id"]: [sheet_reply("v2", i, 0.9)] for i, spec in enumerate(sets)}
    replies["V1"] = ["still not json", "also not json"]  # V1 fails: one try plus the retry
    score = await sheet_scorer(rubric="v2", judge=judge_by_sheet("v2", replies))(
        state(), Target("")
    )

    grade = score.metadata["grade"]
    assert list(score.metadata["failures"]) == ["V1"]
    assert 0 < grade["max"] < 38, "the surviving sheets should still be graded"
    assert grade["accuracy"] == 0.9


async def test_a_total_failure_is_unscored_and_exports_nothing(tmp_path: Path) -> None:
    score = await sheet_scorer(rubric="v2", judge=judge(["nope"]))(state(), Target(""))

    assert math.isnan(score.value)
    assert score.answer == "ungraded"
    assert len(score.metadata["failures"]) == 8

    class _Sample:
        scores = {"sheet_scorer": score}

    class _Log:
        samples = [_Sample()]

    assert export.export(_Log(), out_dir=tmp_path) == []
    assert list(tmp_path.iterdir()) == []


async def test_exported_grade_matches_what_the_standalone_grader_would_write(
    tmp_path: Path,
) -> None:
    """The exporter is the compatibility seam; its output has to be the old file shape."""
    sets, _ = core.load_sheets("v2")
    replies = [sheet_reply("v2", i, 0.5) for i in range(len(sets))]
    score = await sheet_scorer(rubric="v2", judge=judge(replies))(state(), Target(""))

    class _Sample:
        scores = {"sheet_scorer": score}

    class _Log:
        samples = [_Sample()]

    written = export.export(_Log(), out_dir=tmp_path)
    assert [p.name for p in written] == ["graded_r4b10_test_rep1.json"]
    on_disk = json.loads(written[0].read_text())
    assert set(on_disk) >= {
        "report", "title", "grader", "rubric", "total", "max", "per_rubric", "scores",
        "accuracy", "by_mode",
    }
    # and it round-trips through the same aggregation the corpus is checked with
    rebuilt = core.aggregate(
        on_disk["report"], on_disk["title"], on_disk["grader"], on_disk["rubric"],
        on_disk["scores"], on_disk["per_rubric"],
    )
    assert rebuilt == on_disk


def test_unknown_rubric_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown rubric"):
        sheet_scorer(rubric="not-a-rubric")


async def test_sheets_are_graded_concurrently_and_combined_in_sheet_order() -> None:
    sets, _ = core.load_sheets("v2")
    replies = {spec["rubric_id"]: [sheet_reply("v2", i, 0.5)] for i, spec in enumerate(sets)}
    score = await sheet_scorer(rubric="v2", judge=judge_by_sheet("v2", replies))(
        state(), Target("")
    )

    grade = score.metadata["grade"]
    assert list(grade["per_rubric"]) == [spec["rubric_id"] for spec in sets]
    assert grade["max"] == 38 and grade["accuracy"] == 0.5


def test_judge_effort_ladder() -> None:
    from messageboard_audit_bench.grading.scorer import EFFORTS, effort_ladder

    assert effort_ladder() == EFFORTS == ("xhigh", "high", "medium")
    assert effort_ladder("medium") == ("medium",)
    assert effort_ladder("high") == ("high", "medium")
    assert effort_ladder("low") == ("low",)
    with pytest.raises(ValueError):
        effort_ladder("max")


async def test_judge_effort_is_recorded_per_sheet() -> None:
    sets, _ = core.load_sheets("v2")
    replies = {spec["rubric_id"]: [sheet_reply("v2", i, 1.0)] for i, spec in enumerate(sets)}
    score = await sheet_scorer(
        rubric="v2", judge=judge_by_sheet("v2", replies), effort="medium"
    )(state(), Target(""))

    assert {r["effort"] for r in score.metadata["grade"]["per_rubric"].values()} == {"medium"}


def single_reply(mode: str, score: float, drop: set[str] = frozenset()) -> str:
    sets, _ = core.load_sheets(mode)
    items = [
        {"id": c["id"], "score": score, "quote": "", "reason": "x"}
        for s in sets for c in s["claims"] if c["id"] not in drop
    ]
    return json.dumps({"rubric_id": "V", "items": items})


def test_single_prompt_carries_every_point_and_the_answer_key_once() -> None:
    import re

    system, prefix, suffix = core.build_single_prompt("v2", "THE REPORT")
    sets, _ = core.load_sheets("v2")
    claim_ids = [c["id"] for s in sets for c in s["claims"]]

    assert re.findall(r"^## (N\d\d)", prefix, re.M) == claim_ids
    assert prefix.count("**Human incident report (answer key):**") == 1
    assert "Score each of the 38 points" in prefix
    assert suffix.startswith("THE REPORT")
    assert "exactly one item for each of the 38 points" in suffix
    per_sheet = sum(len(core.build_prompt("v2", r, "x")[1]) for r in core.rubric_ids("v2"))
    assert len(prefix) * 5 < per_sheet


def test_single_prompt_refuses_the_tldr_rubric() -> None:
    with pytest.raises(ValueError):
        core.build_single_prompt("tldrh", "x")
    with pytest.raises(ValueError):
        sheet_scorer(rubric="tldrh", single_call=True)


async def test_single_call_grades_every_sheet_in_one_call() -> None:
    calls = []
    reply = single_reply("v2", 0.7)

    def nxt(*_args, **_kwargs) -> ModelOutput:
        calls.append(1)
        return ModelOutput(model="mockllm/model", completion=reply, usage=ModelUsage())

    model = get_model("mockllm/model", custom_outputs=nxt)
    score = await sheet_scorer(rubric="v2", judge=model, single_call=True)(state(), Target(""))

    grade = score.metadata["grade"]
    assert len(calls) == 1
    assert grade["max"] == 38 and grade["accuracy"] == 0.7
    assert grade["grading_calls"] == "single"
    assert list(grade["per_rubric"]) == core.rubric_ids("v2")
    assert "missing_claims" not in grade


async def test_single_call_keeps_returned_claims_and_records_missing_ones() -> None:
    reply = single_reply("v2", 1.0, drop={"N01", "N02", "N03", "N04", "N05", "N38"})
    score = await sheet_scorer(rubric="v2", judge=judge([reply]), single_call=True)(
        state(), Target("")
    )

    grade = score.metadata["grade"]
    assert grade["max"] == 32
    assert grade["missing_claims"] == ["N01", "N02", "N03", "N04", "N05", "N38"]
    assert list(score.metadata["failures"]) == ["V1"]  # V8 kept its other claims
