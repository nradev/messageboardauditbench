#!/usr/bin/env python3
"""Pull the TL;DR out of a model report.

The prompt asks for it at the very top, 200 words maximum, and says the reader may read
nothing else. So it is the part worth scoring on its own: whether a report leads with the
findings, not merely whether it contains them somewhere.

Headings vary — "TL;DR", "1. TL;DR", "## TL;DR", sometimes "Summary" — and a few reports
open with the summary and no heading at all. Everything up to the next section heading is
taken when the first heading is a summary heading; where none is found the opening 200
words stand in, which is what a reader with no time would see anyway.
"""
import re
import sys
from pathlib import Path

SUMMARY = re.compile(r"^\s*(?:\d+[.)]\s*)?(tl;?\s*dr|summary|executive summary|key findings|"
                     r"headline|bottom line)\b", re.I)
HEADING = re.compile(r"^(#{1,3})\s*(.+?)\s*$", re.M)


def extract(text):
    """Return (tldr, how) where how is 'heading', 'lead' or 'whole'."""
    heads = list(HEADING.finditer(text))
    for i, h in enumerate(heads):
        if not SUMMARY.match(h.group(2)):
            continue
        start = h.end()
        # The summary ends at the next heading at the report's section level: its own
        # level, or shallower when the report goes on in shallower sections ("# TL;DR"
        # followed by "## Timeline"). Deeper headings inside "## TL;DR" stay in it.
        later = heads[i + 1:]
        level = max(len(h.group(1)), min((len(k.group(1)) for k in later), default=0))
        nxt = next((k for k in later if len(k.group(1)) <= level), None)
        body = text[start:nxt.start()] if nxt else text[start:]
        if body.strip():
            return body.strip(), "heading"
    # no summary heading: the opening prose up to the first heading, else the first 200 words
    lead = text[:heads[0].start()].strip() if heads else text.strip()
    if len(lead.split()) >= 40:
        return lead, "lead"
    return " ".join(text.split()[:200]), "whole"


def main():
    for a in sys.argv[1:]:
        t, how = extract(Path(a).read_text())
        print(f"== {Path(a).name}  [{how}, {len(t.split())} words]\n{t[:400]}\n")


if __name__ == "__main__":
    main()
