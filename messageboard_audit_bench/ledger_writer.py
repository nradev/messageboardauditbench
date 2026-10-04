"""Optional final report synthesis from an investigator's evidence ledger."""

from __future__ import annotations

from dataclasses import dataclass

from inspect_ai.model import (
    ChatMessageSystem,
    ChatMessageUser,
    GenerateConfig,
    Model,
)

LEDGER_PATH = "/work/evidence_ledger.md"
LEDGER_PROMPT = (
    "\n\nThis arm reserves the final 24% of the same wall-clock budget for a fresh "
    "writing pass. Keep /work/evidence_ledger.md as a compact record of candidate findings while you "
    "investigate. For each candidate, write the claim, the exact record IDs and a short "
    "verbatim excerpt supporting it, "
    "your confidence, and whether it is in report.md. Include important uncertainty or "
    "counterevidence. Use only records you actually examined. Keep writing report.md as usual; "
    "the final writing pass will use both files.\n"
)


@dataclass(frozen=True)
class WriterInput:
    draft: str
    ledger: str
    minimum_words: int
    maximum_words: int


@dataclass(frozen=True)
class WriterResult:
    report: str
    model: str
    input_tokens: int
    output_tokens: int


def writer_messages(value: WriterInput) -> list[ChatMessageSystem | ChatMessageUser]:
    """Give a fresh model only the draft and evidence ledger, without the investigation trace."""
    target_words = value.minimum_words + (value.maximum_words - value.minimum_words) // 2
    return [
        ChatMessageSystem(content=(
            "You are the final writer of an evidence-based incident report. Use only the "
            "draft and evidence ledger supplied here. Do not invent facts, quotes, record IDs, "
            "or sources. Retain supported findings already in the draft and add important "
            "ledger findings that the draft omitted. Preserve uncertainty and counterevidence. "
            "Return only the complete Markdown report, with a TL;DR of at most 200 words, "
            "then a timeline and analysis."
        )),
        ChatMessageUser(content=(
            f"Target about {target_words} words; allowed length: "
            f"{value.minimum_words} to {value.maximum_words} words. "
            "The upper limit is strict. Cite record IDs for factual claims.\n\n"
            f"CURRENT DRAFT:\n{value.draft}\n\nEVIDENCE LEDGER:\n{value.ledger}"
        )),
    ]


async def write_final_report(value: WriterInput, model: Model) -> WriterResult:
    """Use the trial's policy model for one bounded final writing call."""
    output = await model.generate(
        writer_messages(value), config=GenerateConfig(max_tokens=12000, reasoning_effort="medium")
    )
    report = (output.completion or "").strip()
    if not report:
        raise ValueError("ledger writer returned an empty report")
    return WriterResult(
        report=report + "\n",
        model=str(model),
        input_tokens=(output.usage.input_tokens or 0) if output.usage else 0,
        output_tokens=(output.usage.output_tokens or 0) if output.usage else 0,
    )
