"""Final writer: a fresh model call rewrites the agent's draft report at the end of a run.

Agents build their reports through many small edits inside a long context, and findings
they saw are left out or crowded out under the word cap. The writer sees the draft and
the material the harness already holds at once, in a clean context, and rebalances the
report. It does not investigate: everything it may cite is in its inputs.

Strength (``-T writer_strength=edit|rebalance|rewrite``) sets how far the writer may depart
from the draft: light edits, making room for material the draft leaves out, or a fresh
report built around the most important findings of all inputs.

Levels (``-T writer=W1|W2|W3``, see ``atlas writer pack``):
  W1  the draft and its gap check (Fix and Consider items)
  W2  + verified reader notes the draft does not use (crew arms)
  W3  + excerpts of records the draft cites or the agent read at length, and a short
        structural map of the corpus (context only)

Guardrails: the rewrite is checked (``atlas writer check``) for Fix items the draft did
not have, record refs that appear nowhere in the inputs, and the word limits; one repair
call may fix those, otherwise the draft stays. Any failure keeps the draft; the writer
never fails the sample. Its tokens count toward the sample's budget like every model's.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field

from inspect_ai.model import (
    ChatMessageSystem,
    ChatMessageUser,
    GenerateConfig,
    get_model,
)
from inspect_ai.util import sandbox

from messageboard_audit_bench.investigation_tools import ATLAS_BIN, install_atlas
from messageboard_audit_bench.report_length import count_words

LEVELS = ("W1", "W2", "W3")
DRAFT_PATH = "/work/report.draft.md"
CANDIDATE_PATH = "/tmp/mbab-writer-candidate.md"
INPUTS_PATH = "/tmp/mbab-writer-inputs.md"
MIN_TOKENS = 4000  # the least a writer call is given, even when the reserve is nearly spent
FIRST_CALL_SHARE = 0.6  # of the tokens left: the first call leaves room for one repair call
STRENGTHS = ("edit", "rebalance", "rewrite")
MAX_TRIMS = 2  # trimming rounds when only the length is wrong
TARGET_SHARE = 0.9  # of the word limit: models overshoot word counts, and an overlong report is rejected
_REPORT = re.compile(r"<report>\s*(.*?)\s*</report>", re.S)

# How far the writer may depart from the draft (``writer_strength``). Each replaces the first
# rule of the system prompt; the other rules hold at every strength.
APPROACH = {
    "edit": (
        "Keep every supported finding of the draft unless it is immaterial; you may compress, "
        "merge, reorder and restructure. Put the most important findings first and make the "
        "summary reflect them."
    ),
    "rebalance": (
        "Use the draft as the backbone, but rebalance it: weigh each finding of the draft and "
        "each item of the other inputs by its importance to the account. When the report is "
        "near the word limit, compress or cut the least important material (repetition, minor "
        "detail, long lists, general commentary) to make room for material findings from the "
        "gap check, reader notes or excerpts that the draft leaves out. Keep the draft's major "
        "findings. Put the most important findings first and make the summary reflect them."
    ),
    "rewrite": (
        "Write the report anew. The draft is the investigator's account and your main source, "
        "but not a template: decide from all the inputs which findings matter most, and select, "
        "organise and word the report around them. Keep the draft's major supported findings; "
        "cut or compress whatever matters less than material the draft leaves out. Put the most "
        "important findings first and make the summary reflect them."
    ),
}

SYSTEM = (
    "You are the final writer of an investigation report. An investigator examined a corpus "
    "of records within a fixed budget and wrote the draft below. You did not see the corpus; "
    "you see the draft and material the investigation tools collected. Write the final "
    "report in one pass.\n\n"
    "Rules:\n"
    "- {approach}\n"
    "- Correct every Fix item: each names a citation or quote the data does not support as "
    "written. Fix it with what the inputs support, or drop the claim.\n"
    "- Consider items, reader notes and record excerpts are optional: add something only if "
    "it is material to the account and supported by the text given here.\n"
    "- Cite every factual claim with record refs that appear in the inputs, in the draft's "
    "citation style. Quote only text that appears verbatim in the inputs. Do not add "
    "outside knowledge, and do not invent records, quotes, numbers or dates. Quotes must match "
    "the text exactly, word for word.\n"
    "- When a Consider item asks whether a passage has enough support and the inputs hold no "
    "record that supports it, do not add a citation: keep the passage as it is, mark it as "
    "an inference, or cut it.\n"
    "- Mark inferences as inferences.\n"
    "- The corpus map, where given, is context for proportion and emphasis only: do not cite "
    "it, and do not state a fact that rests on it alone.\n"
    "- Follow the report requirements of the investigator's instructions, including the word "
    "limits. Their parts about tools, budgets and process applied to the investigation, not "
    "to you.\n"
    "- Reply with the complete final report in Markdown between <report> and </report>, and "
    "nothing else."
)


def parse_variants(value: str | list | tuple | None) -> tuple[tuple[str, str], ...]:
    """``"W3:edit,W3:rewrite"`` (or a list, as Inspect's CLI passes it) -> ((level, strength), ...).
    A bare level means ``edit``."""
    parts = value if isinstance(value, (list, tuple)) else (value or "").split(",")
    out = []
    for p in (str(x).strip() for x in parts):
        if not p:
            continue
        level, _, strength = p.partition(":")
        strength = strength or "edit"
        if level not in LEVELS or strength not in STRENGTHS:
            raise ValueError(f"writer variant {p!r}: use LEVEL:STRENGTH with LEVEL in {', '.join(LEVELS)} and "
                             f"STRENGTH in {', '.join(STRENGTHS)}")
        out.append((level, strength))
    if len(set(out)) != len(out):
        raise ValueError("writer variants must be distinct")
    return tuple(out)


def variant_name(level: str, strength: str) -> str:
    return f"{level}-{strength}"


@dataclass
class WriterResult:
    report: str | None  # the final report, or None to keep the draft
    meta: dict = field(default_factory=dict)


def _section(title: str, body: str) -> str:
    return f"\n\n## {title}\n\n{body.strip()}" if body.strip() else ""


def build_inputs(task_prompt: str, draft: str, pack: dict, min_words: int, max_words: int) -> str:
    limits = (f"The final report must have at most {max_words:,} words" if max_words else "")
    if limits and min_words:
        limits += f" and at least {min_words:,}"
    if limits:
        limits += (f"; aim for about {target_words(min_words, max_words):,}, since a report over the limit is "
                   "rejected")
    parts = ["# Inputs for the final report"]
    parts.append(_section("The investigator's instructions", task_prompt))
    if limits:
        parts.append(_section("Length", limits + "."))
    parts.append(_section("The draft report", draft))
    parts.append(_section("Gap check of the draft (atlas gapcheck)", pack.get("gapcheck", "")))
    notes = pack.get("notes") or []
    if notes:
        parts.append(_section("Verified reader notes the draft does not use (leads, quote-checked; most "
                              "salient records first)", "\n".join(f"- {n}" for n in notes)))
    for key, title in (("cited", "Excerpts of the records the draft cites"),
                       ("opened", "Excerpts of records the investigator read but did not cite")):
        recs = pack.get(key) or []
        if recs:
            parts.append(_section(title, "\n\n".join(f"[{r['ref']}]\n{r['text']}" for r in recs)))
    if pack.get("map"):
        parts.append(_section("Corpus map (context only: never cite it)", pack["map"]))
    return "".join(parts)


def target_words(min_words: int, max_words: int) -> int:
    """The length the writer is asked to aim for, below the limit (models overshoot)."""
    return max(min_words, round(max_words * TARGET_SHARE))


def trim_target(min_words: int, max_words: int) -> int:
    """The length trimming stops at: just under the limit, since deletions are counted exactly."""
    return max(min_words, max_words - max(20, round(max_words * 0.01)))


def extract_report(text: str) -> str | None:
    m = _REPORT.search(text or "")
    if m:
        return m.group(1).strip() or None
    # A reply that is plainly a report without the tags is accepted; anything else is not.
    body = (text or "").strip()
    body = re.sub(r"^```(?:markdown|md)?\s*\n(.*?)\n```$", r"\1", body, flags=re.S)
    return body if body.startswith("#") else None


def content_issues(check: dict) -> list[str]:
    out = [f"Unsupported as written (the draft did not have this problem): {t}" for t in check.get("new_fix") or []]
    outside = check.get("refs_outside") or []
    if outside:
        out.append("It cites records that appear nowhere in your inputs, so you cannot have read them: "
                   + ", ".join(outside[:20]) + ". Remove or replace those citations.")
    return out


def length_issue(report: str, min_words: int, max_words: int) -> str | None:
    words = count_words(report)
    if max_words and words > max_words:
        return f"It has {words:,} words, above the limit of {max_words:,}."
    if min_words and words < min_words:
        return f"It has {words:,} words, below the minimum of {min_words:,}."
    return None


# ---------- trimming to the word limit ----------

_LIST_ITEM = re.compile(r"\s*(?:[-*+]|\d+[.)])\s")
_HEADING = re.compile(r"#{1,6}\s")
_SUMMARY = re.compile(r"tl;?dr|summary", re.I)


def units(report: str) -> list[dict]:
    """The report as units in order: headings, paragraphs and list items (each with its
    continuation lines and trailing blank lines). Headings and the units of a summary
    section (a heading naming a TL;DR or summary) are fixed; the rest may be deleted."""
    out: list[dict] = []
    in_summary = False
    prev_blank = True
    for line in report.split("\n"):
        blank = not line.strip()
        if _HEADING.match(line):
            in_summary = bool(_SUMMARY.search(line))
            out.append({"lines": [line], "fixed": True})
        elif blank:
            if out:
                out[-1]["lines"].append(line)
            else:
                out.append({"lines": [line], "fixed": True})
        elif prev_blank or _LIST_ITEM.match(line) or (out and out[-1]["fixed"] and not in_summary):
            out.append({"lines": [line], "fixed": in_summary})
        else:
            out[-1]["lines"].append(line)
        prev_blank = blank
    return out


def trim_prompt(report: str, us: list[dict], cut: int) -> str:
    rows = []
    for k, u in enumerate(us):
        text = "\n".join(u["lines"]).strip()
        if not text:
            continue
        rows.append(f"(fixed) {text}" if u["fixed"] else f"[{k}] ({count_words(text)} words) {text}")
    return (f"This report has {count_words(report):,} words and must lose at least {cut:,} words. It is shown "
            "below as numbered units (paragraphs and list items) with their word counts; headings and the "
            "summary are fixed. Choose whole units to delete, the least important first: repetition, minor "
            "detail, general commentary. Keep the most important findings and their evidence. Reply with only "
            "the numbers of the units to delete, separated by commas.\n\n" + "\n\n".join(rows))


def apply_trim(us: list[dict], reply: str, target: int, floor: int = 0) -> tuple[str, int]:
    """Delete the units the reply names, in its order (least important first, as asked),
    until the report is down to ``target`` words; never below ``floor``, and never a fixed
    unit."""
    def text(units_):
        return "\n".join(line for u in units_ for line in u["lines"]).strip() + "\n"

    keep = list(us)
    removed = 0
    for n in dict.fromkeys(int(x) for x in re.findall(r"\d+", reply or "")):
        if count_words(text(keep)) <= target:
            break
        if not 0 <= n < len(us) or us[n]["fixed"] or us[n] not in keep:
            continue
        trial = [u for u in keep if u is not us[n]]
        if count_words(text(trial)) < floor:
            continue
        keep = trial
        removed += 1
    return text(keep), removed


async def _atlas_json(argv: list[str]) -> dict:
    result = await sandbox().exec([ATLAS_BIN, *argv], timeout=300)
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        raise RuntimeError(f"atlas {argv[0]} {argv[1]} failed: {(result.stderr or result.stdout)[:300]}")
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"atlas {argv[0]} {argv[1]}: {data['error']}")
    return data


async def _write(path: str, text: str) -> None:
    # Through exec, so files under /work belong to the agent's uid.
    res = await sandbox().exec(["sh", "-c", f"cat > {path}"], input=text, timeout=60)
    if not res.success:
        raise RuntimeError(f"could not write {path}: {res.stderr[:300]}")


def system_prompt(strength: str) -> str:
    return SYSTEM.replace("{approach}", APPROACH[strength])


async def run_writer(level: str, task_prompt: str, draft: str, *, min_words: int, max_words: int,
                     tokens_left, deadline: float, install: bool, strength: str = "edit",
                     cache: dict | None = None) -> WriterResult:
    """Rewrite ``draft``. ``tokens_left()`` is the output-token allowance left for the writer
    (None on a time budget); ``deadline`` is a ``time.monotonic()`` value. ``cache``, shared
    by several writer runs on the same draft, keeps the atlas install and each level's
    inputs so they are prepared once."""
    cache = {} if cache is None else cache
    started = time.monotonic()
    meta: dict = {"level": level, "strength": strength, "draft_words": count_words(draft), "calls": 0,
                  "output_tokens": 0, "input_tokens": 0}

    def finish(report: str | None, status: str, **extra) -> WriterResult:
        meta.update(status=status, seconds=round(time.monotonic() - started, 1), **extra)
        return WriterResult(report, meta)

    if not draft.strip():
        return finish(None, "skipped", reason="no draft report")
    try:
        if install and not cache.get("installed"):
            await install_atlas()
            cache["installed"] = True
        await _write(DRAFT_PATH, draft)
        pack = cache.get(level) or await _atlas_json(["writer", "pack", DRAFT_PATH, "--level", level])
        cache[level] = pack
        meta.update(draft_fix=pack.get("fix"), draft_consider=pack.get("consider"),
                    notes_given=len(pack.get("notes") or []), notes=pack.get("notes") or [], cited_given=len(pack.get("cited") or []),
                    opened_given=len(pack.get("opened") or []))
        inputs = build_inputs(task_prompt, draft, pack, min_words, max_words)
        await _write(INPUTS_PATH, inputs)
        meta["input_chars"] = len(inputs)
        model = get_model(role="writer", default=get_model())
        meta["model"] = str(model)
        messages = [ChatMessageSystem(content=system_prompt(strength)), ChatMessageUser(content=inputs)]

        async def call() -> str | None:
            left = tokens_left() if tokens_left is not None else None
            if left is not None and meta["calls"] == 0:
                left = round(left * FIRST_CALL_SHARE)  # keep room for a repair call
            config = GenerateConfig(max_tokens=max(MIN_TOKENS, left)) if left is not None else GenerateConfig()
            remaining = deadline - time.monotonic()
            if remaining < 10:
                raise TimeoutError("no time left for the writer")
            out = await asyncio.wait_for(model.generate(messages, config=config), timeout=remaining)
            meta["calls"] += 1
            if out.usage:
                meta["output_tokens"] += out.usage.output_tokens or 0
                meta["input_tokens"] += out.usage.input_tokens or 0
            messages.append(out.message)
            return extract_report(out.completion)

        async def assess(report: str) -> dict:
            await _write(CANDIDATE_PATH, report)
            return await _atlas_json(["writer", "check", CANDIDATE_PATH, "--draft", DRAFT_PATH,
                                      "--inputs", INPUTS_PATH])

        async def trim(report: str) -> str:
            """Ask which units to delete to reach the target, and delete them here: models do
            not count words reliably, and a deletion cannot add an unsupported claim."""
            us = units(report)
            cut = count_words(report) - trim_target(min_words, max_words)
            left = tokens_left() if tokens_left is not None else None
            config = GenerateConfig(max_tokens=max(MIN_TOKENS, left)) if left is not None else GenerateConfig()
            remaining = deadline - time.monotonic()
            if remaining < 10:
                raise TimeoutError("no time left for the writer")
            out = await asyncio.wait_for(model.generate([ChatMessageUser(content=trim_prompt(report, us, cut))],
                                                        config=config), timeout=remaining)
            meta["calls"] += 1
            if out.usage:
                meta["output_tokens"] += out.usage.output_tokens or 0
                meta["input_tokens"] += out.usage.input_tokens or 0
            trimmed, removed = apply_trim(us, out.completion, trim_target(min_words, max_words), min_words)
            meta["units_deleted"] = meta.get("units_deleted", 0) + removed
            return trimmed

        report = await call()
        if report is None:
            return finish(None, "kept_draft", reason="the writer's reply held no report")
        check = await assess(report)
        issues = content_issues(check)
        meta["first_issues"] = issues + [x for x in [length_issue(report, min_words, max_words)] if x]
        meta["repairs"] = meta["trims"] = 0
        if issues:
            # One repair call for content problems; length is handled by trimming below.
            repair = ("Your report has these problems:\n" + "\n".join(f"- {i}" for i in issues)
                      + "\nFix them and stay within the length. Reply with the corrected complete report "
                      "between <report> and </report>.")
            messages.append(ChatMessageUser(content=repair))
            # The repair names records too (where a misattributed quote really is): they are
            # inputs now, so citing them is not citing unseen records.
            await _write(INPUTS_PATH, inputs + _section("Repair notes", repair))
            meta["repairs"] = 1
            report = await call()
            if report is None:
                return finish(None, "kept_draft", reason="the repair reply held no report")
            check = await assess(report)
            issues = content_issues(check)
            if issues:
                return finish(None, "kept_draft", reason="problems remained after one repair", issues=issues)
        while max_words and count_words(report) > max_words and meta["trims"] < MAX_TRIMS:
            meta["trims"] += 1
            report = await trim(report)
        problem = length_issue(report, min_words, max_words)
        if problem:
            return finish(None, "kept_draft", reason="outside the word limits", issues=[problem])
        if meta["trims"]:
            check = await assess(report)
            if content_issues(check):  # a deletion should not add any; checked all the same
                return finish(None, "kept_draft", reason="problems after trimming", issues=content_issues(check))
        return finish(report, "replaced", final_words=count_words(report), final_fix=check.get("fix"))
    except Exception as e:  # never fail the sample: keep the draft
        return finish(None, "error", reason=f"{type(e).__name__}: {e}"[:500])


# ---------- grading the variants ----------


def variant_scorer(inner, variant: str, name: str):
    """Score writer variant ``variant``'s report (from ``writer_variants`` metadata) with the
    ``inner`` scorer, as if it were the sample's output. Samples without the variant (the
    agent refused, or the run failed before the writer) are unscored."""
    import copy

    from inspect_ai.model import ModelOutput
    from inspect_ai.scorer import Score, mean, scorer, stderr

    @scorer(metrics=[mean(), stderr()], name=name)
    def _variant():
        async def score(state, target):
            entry = (state.metadata.get("writer_variants") or {}).get(variant)
            if not entry or entry.get("report") is None:
                return Score.unscored(reason=f"no writer variant {variant}", answer="ungraded")
            view = copy.copy(state)
            view.output = ModelOutput.from_content(model="writer", content=entry["report"])
            result = await inner(view, target)
            if result is not None:
                result.metadata = {**(result.metadata or {}), "writer_variant": variant,
                                   "replaced": entry.get("replaced")}
            return result

        return score

    return _variant()
