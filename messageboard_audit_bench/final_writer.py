"""Final writer: a fresh model call rewrites the agent's draft report at the end of a run.

Agents build their reports through many small edits inside a long context, and findings
they saw are left out or crowded out under the word cap. The writer sees the draft and
the material the harness already holds at once, in a clean context, and rebalances the
report. It does not investigate: everything it may cite is in its inputs.

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
_REPORT = re.compile(r"<report>\s*(.*?)\s*</report>", re.S)

SYSTEM = (
    "You are the final writer of an investigation report. An investigator examined a corpus "
    "of records within a fixed budget and wrote the draft below. You did not see the corpus; "
    "you see the draft and material the investigation tools collected. Write the final "
    "report in one pass.\n\n"
    "Rules:\n"
    "- Keep every supported finding of the draft unless it is immaterial; you may compress, "
    "merge, reorder and restructure. Put the most important findings first and make the "
    "summary reflect them.\n"
    "- Correct every Fix item: each names a citation or quote the data does not support as "
    "written. Fix it with what the inputs support, or drop the claim.\n"
    "- Consider items, reader notes and record excerpts are optional: add something only if "
    "it is material to the account and supported by the text given here.\n"
    "- Cite every factual claim with record refs that appear in the inputs, in the draft's "
    "citation style. Quote only text that appears verbatim in the inputs. Do not add "
    "outside knowledge, and do not invent records, quotes, numbers or dates.\n"
    "- Mark inferences as inferences.\n"
    "- The corpus map, where given, is context for proportion and emphasis only: do not cite "
    "it, and do not state a fact that rests on it alone.\n"
    "- Follow the report requirements of the investigator's instructions, including the word "
    "limits. Their parts about tools, budgets and process applied to the investigation, not "
    "to you.\n"
    "- Reply with the complete final report in Markdown between <report> and </report>, and "
    "nothing else."
)


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


def extract_report(text: str) -> str | None:
    m = _REPORT.search(text or "")
    if m:
        return m.group(1).strip() or None
    # A reply that is plainly a report without the tags is accepted; anything else is not.
    body = (text or "").strip()
    body = re.sub(r"^```(?:markdown|md)?\s*\n(.*?)\n```$", r"\1", body, flags=re.S)
    return body if body.startswith("#") else None


def problems(check: dict, words: int, min_words: int, max_words: int) -> list[str]:
    out = []
    if max_words and words > max_words:
        out.append(f"It has {words:,} words, above the limit of {max_words:,}: shorten it.")
    if min_words and words < min_words:
        out.append(f"It has {words:,} words, below the minimum of {min_words:,}.")
    for t in check.get("new_fix") or []:
        out.append(f"Unsupported as written (the draft did not have this problem): {t}")
    outside = check.get("refs_outside") or []
    if outside:
        out.append("It cites records that appear nowhere in your inputs, so you cannot have read them: "
                   + ", ".join(outside[:20]) + ". Remove or replace those citations.")
    return out


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


async def run_writer(level: str, task_prompt: str, draft: str, *, min_words: int, max_words: int,
                     tokens_left, deadline: float, install: bool) -> WriterResult:
    """Rewrite ``draft``. ``tokens_left()`` is the output-token allowance left for the writer
    (None on a time budget); ``deadline`` is a ``time.monotonic()`` value."""
    started = time.monotonic()
    meta: dict = {"level": level, "draft_words": count_words(draft), "calls": 0,
                  "output_tokens": 0, "input_tokens": 0}

    def finish(report: str | None, status: str, **extra) -> WriterResult:
        meta.update(status=status, seconds=round(time.monotonic() - started, 1), **extra)
        return WriterResult(report, meta)

    if not draft.strip():
        return finish(None, "skipped", reason="no draft report")
    try:
        if install:
            await install_atlas()
        await _write(DRAFT_PATH, draft)
        pack = await _atlas_json(["writer", "pack", DRAFT_PATH, "--level", level])
        meta.update(draft_fix=pack.get("fix"), draft_consider=pack.get("consider"),
                    notes_given=len(pack.get("notes") or []), notes=pack.get("notes") or [], cited_given=len(pack.get("cited") or []),
                    opened_given=len(pack.get("opened") or []))
        inputs = build_inputs(task_prompt, draft, pack, min_words, max_words)
        await _write(INPUTS_PATH, inputs)
        meta["input_chars"] = len(inputs)
        model = get_model(role="writer", default=get_model())
        meta["model"] = str(model)
        messages = [ChatMessageSystem(content=SYSTEM), ChatMessageUser(content=inputs)]

        async def call() -> str | None:
            left = tokens_left() if tokens_left is not None else None
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

        async def assess(report: str) -> tuple[dict, list[str]]:
            await _write(CANDIDATE_PATH, report)
            check = await _atlas_json(["writer", "check", CANDIDATE_PATH, "--draft", DRAFT_PATH,
                                       "--inputs", INPUTS_PATH])
            return check, problems(check, count_words(report), min_words, max_words)

        report = await call()
        if report is None:
            return finish(None, "kept_draft", reason="the writer's reply held no report")
        check, issues = await assess(report)
        meta["first_issues"] = issues
        if issues:
            messages.append(ChatMessageUser(content=(
                "Your report has these problems:\n" + "\n".join(f"- {i}" for i in issues)
                + "\nReply with the corrected complete report between <report> and </report>.")))
            report = await call()
            if report is None:
                return finish(None, "kept_draft", reason="the repair reply held no report")
            check, issues = await assess(report)
            if issues:
                return finish(None, "kept_draft", reason="problems remained after one repair", issues=issues)
        return finish(report, "replaced", final_words=count_words(report), final_fix=check.get("fix"))
    except Exception as e:  # never fail the sample: keep the draft
        return finish(None, "error", reason=f"{type(e).__name__}: {e}"[:500])
