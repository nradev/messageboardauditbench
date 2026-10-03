"""The rubric judge, as an Inspect scorer.

Every sheet of a report is graded concurrently (results are combined in sheet order), with
the same bytes the standalone grader sends, through Inspect's model
layer instead of a hand-rolled thread pool — so the judge's calls land in the eval log,
count against the run's token accounting, and obey `--max-connections` like everything
else.

The whole aggregate dict goes into `Score.metadata`, not just the number. That is what lets
`export.py` reproduce `benchmark/graded/.../graded_<key>.json` byte for byte, which in turn
is why no figure, viewer or analysis script has to change.

Failure is per sheet, never fatal: one unparseable response should not throw away the other
seven sheets' claims. But a report where *every* sheet failed is not a report that scored
zero — an auth or billing error hits all of them at once — so that case returns
`Score.unscored` and the exporter writes nothing. The standalone grader learned this the
expensive way, after a lapsed Anthropic balance wrote 112 files recording a total of 0.
"""

from __future__ import annotations

import asyncio
from typing import Any

from inspect_ai.model import (
    ChatMessageSystem,
    ChatMessageUser,
    ContentText,
    GenerateConfig,
    Model,
    get_model,
)
from inspect_ai.scorer import Score, Scorer, Target, mean, scorer, stderr
from inspect_ai.solver import TaskState

from messageboard_audit_bench.grading import core

# Tried in order, dropping down when a provider rejects the level outright. Mirrors the
# standalone grader's ladder.
EFFORTS = ("xhigh", "high", "medium")
JUDGE_EFFORTS = (*EFFORTS, "low")


def effort_ladder(effort: str | None = None) -> tuple[str, ...]:
    """The efforts to try, starting at ``effort`` (default: the full ladder from xhigh).

    A lower starting effort is faster and cheaper but scores differently from the
    published xhigh grades, so compare only grades made at the same effort.
    """
    if effort is None:
        return EFFORTS
    if effort not in JUDGE_EFFORTS:
        raise ValueError(f"judge effort must be one of {', '.join(JUDGE_EFFORTS)}, not {effort!r}")
    # Drop down only as far as the published ladder does (medium), unless asked for low.
    return EFFORTS[EFFORTS.index(effort):] if effort in EFFORTS else ("low",)


def _is_anthropic(model: Model) -> bool:
    return str(model).startswith("anthropic/") or "claude" in str(model)


def _config(model: Model, effort: str) -> GenerateConfig:
    """Effort goes in a different field per provider, and only Anthropic takes `effort`."""
    if _is_anthropic(model):
        return GenerateConfig(effort=effort, max_tokens=32000)
    return GenerateConfig(reasoning_effort=effort, max_tokens=16000)


def _messages(system: str, prefix: str, suffix: str) -> list[Any]:
    """Sheet first, report second, always.

    Inspect's ContentText carries no cache_control, so the explicit breakpoint the
    standalone grader sets is not reproducible here. Anthropic's automatic caching covers
    it instead: it caches the longest matching prefix, and the prefix — sheet plus answer
    key, ~90 kB — is identical for every report graded on this sheet. Reversing these two
    blocks would silently cost that.
    """
    return [
        ChatMessageSystem(content=system),
        ChatMessageUser(content=[ContentText(text=prefix), ContentText(text=suffix)]),
    ]


async def grade_sheet(
    model: Model,
    mode: str,
    rubric_id: str,
    report_md: str,
    templates: dict[str, str],
    variant: str | None = None,
    efforts: tuple[str, ...] = EFFORTS,
) -> tuple[dict[str, dict], str]:
    """One sheet. Returns (claims, effort actually used); raises only if nothing parses."""
    spec = core.MODES[mode]
    system, prefix, suffix = core.build_prompt(mode, rubric_id, report_md, templates, variant)
    return await _grade_prompt(model, f"{mode}/{rubric_id}", system, prefix, suffix, spec, efforts)


async def _grade_prompt(
    model: Model,
    label: str,
    system: str,
    prefix: str,
    suffix: str,
    spec: core.ModeSpec,
    efforts: tuple[str, ...],
) -> tuple[dict[str, dict], str]:
    """One judge call (with the effort ladder and one JSON retry) -> (claims, effort)."""
    last: Exception | None = None
    for effort in efforts:
        try:
            out = await model.generate(_messages(system, prefix, suffix), config=_config(model, effort))
        except Exception as exc:  # noqa: BLE001 — an effort the provider rejects, or a real error
            last = exc
            if "effort" in str(exc).lower():
                continue
            raise
        data = core.extract_json(out.completion)
        if data is None:
            # one retry, telling it to drop the wrapper
            out = await model.generate(
                _messages(system, prefix, suffix + core.JSON_ONLY),
                config=_config(model, effort),
            )
            data = core.extract_json(out.completion)
        if data is None:
            raise ValueError(
                f"unparseable JSON from {model} on {label}: {out.completion[:200]!r}"
            )
        return core.parse_items(data, spec.lo, spec.hi), effort
    raise RuntimeError(f"all efforts failed for {label}: {last!r}")


async def _grade_single_call(
    model: Model,
    mode: str,
    report_md: str,
    templates: dict[str, str],
    variant: str | None,
    efforts: tuple[str, ...],
    sets: list[dict],
) -> list[tuple[dict[str, dict], str] | Exception]:
    """Every sheet in one judge call, split back into one result per sheet.

    A sheet none of whose claims came back is failed; one with some claims missing keeps
    the claims that came back (`missing_claims` in the grade records the rest).
    """
    system, prefix, suffix = core.build_single_prompt(mode, report_md, templates, variant)
    try:
        items, effort = await _grade_prompt(
            model, f"{mode}/single", system, prefix, suffix, core.MODES[mode], efforts
        )
    except Exception as exc:  # noqa: BLE001 — every sheet fails together
        return [exc] * len(sets)
    results: list[tuple[dict[str, dict], str] | Exception] = []
    for spec in sets:
        got = {c["id"]: items[c["id"]] for c in spec["claims"] if c["id"] in items}
        results.append(
            (got, effort) if got else ValueError("single-call reply has none of this sheet's claims")
        )
    return results


@scorer(metrics=[mean(), stderr()])
def sheet_scorer(
    rubric: str = "v2",
    judge: str | Model | None = None,
    variant: str | None = None,
    effort: str | None = None,
    single_call: bool = False,
) -> Scorer:
    """Grade the report against every sheet of `rubric`.

    Args:
      rubric: a key of `core.MODES`, including the wiki's "v2"/"tldrh" and
        Mythos 5's "m5"/"m5tldrh" and RubyHack's "rh"/"rhtldrh" pairs.
      judge: the grading model. Resolved at scoring time through the `grader` model role,
        so `--model-role grader=openai/gpt-5.6-sol` works as it does for the other scorers.
      variant: a rubric variant (`core.VARIANTS`); the task derives it from the data
        variant, so a verbatim_anthropic run is judged against the swapped answer key.
      effort: the judge's starting reasoning effort (default xhigh, as published; see
        `effort_ladder`). The effort actually used is recorded per sheet.
      single_call: grade every sheet in one judge call (`core.build_single_prompt`)
        instead of one call per sheet: about 7x less input and one latency. Only for
        multi-sheet finding rubrics (`core.SINGLE_CALL_MODES`); the grade records
        `grading_calls: "single"`, and its scores are not interchangeable with per-sheet
        grades.
    """
    efforts = effort_ladder(effort)
    if single_call and rubric not in core.SINGLE_CALL_MODES:
        raise ValueError(
            f"single_call supports {sorted(core.SINGLE_CALL_MODES)}, not {rubric!r}; the TL;DR "
            "rubrics stay separate so the judge sees only the summary"
        )
    if rubric not in core.MODES:
        raise ValueError(f"unknown rubric {rubric!r}; expected one of {sorted(core.MODES)}")
    core.load_sheets(rubric, variant)  # Validate assets before launching an agent.

    async def score(state: TaskState, target: Target) -> Score:
        selected_variant = variant or core.variant_for_data(state.metadata.get("data_variant"))
        sets, templates = core.load_sheets(rubric, selected_variant)
        model = get_model(judge, role="grader")
        report_md = state.output.completion if state.output else ""
        key = str(state.sample_id)
        title = str(state.metadata.get("title", key))

        per_claim: dict[str, dict] = {}
        per_rubric: dict[str, dict] = {}
        failures: dict[str, str] = {}
        # All sheets at once (bounded by --max-connections); exceptions are kept per
        # sheet and the results combined in sheet order, as the sequential loop did.
        if single_call:
            results = await _grade_single_call(
                model, rubric, report_md, templates, selected_variant, efforts, sets
            )
        else:
            results = await asyncio.gather(
                *(
                    grade_sheet(
                        model, rubric, spec["rubric_id"], report_md, templates,
                        selected_variant, efforts,
                    )
                    for spec in sets
                ),
                return_exceptions=True,
            )
        for spec, result in zip(sets, results, strict=True):
            rubric_id = spec["rubric_id"]
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result  # cancellation and the like are not sheet failures
                failures[rubric_id] = f"{type(result).__name__}: {result}"[:300]
                continue
            items, used_effort = result
            per_claim.update(items)
            per_rubric[rubric_id] = {
                "score": round(sum(i["score"] for i in items.values()), 2),
                "max": len(items),
                "effort": used_effort,
            }

        # recorded bare, the way every existing grade file records it
        out = core.aggregate(
            key, title, core.judge_name(str(model)), rubric, per_claim, per_rubric, sets, selected_variant
        )
        if single_call and out["max"]:
            out["grading_calls"] = "single"
            expected = [c["id"] for spec in sets for c in spec["claims"]]
            missing = [cid for cid in expected if cid not in per_claim]
            if missing:
                out["missing_claims"] = missing
        if out["max"] == 0:
            return Score.unscored(
                reason="every sheet failed",
                answer="ungraded",
                explanation="; ".join(f"{k}: {v}" for k, v in failures.items())[:600],
                metadata={"grader": str(model), "rubric": rubric, "failures": failures},
            )

        value = out["contradiction"] if rubric == "contradiction" else out["accuracy"]
        return Score(
            value=value,
            answer=f"{out['total']}/{out['max']}",
            explanation=(
                f"{len(per_rubric)}/{len(sets)} sheets graded by {model}"
                + (f"; failed: {', '.join(failures)}" if failures else "")
            ),
            metadata={"grade": out, "failures": failures, "variant": selected_variant},
        )

    return score
