"""The four metrics for the agent's report and each writer variant, from .eval logs.

    uv run python tools/writer_variants_metrics.py logs/atlas-tok200k-variants

For each sample: raw findings (mean per-finding credit), coverage (mean of max(2s-1, 0)),
TL;DR and combined (0.7 coverage + 0.3 TL;DR), for the agent's report (`draft`) and each
variant, with whether the variant replaced the draft and what it cost. Then the means per
variant, and per finding how often each variant gained or lost credit against the draft
of the same sample (the comparison is paired: every variant rewrites the same draft).
"""

from __future__ import annotations

import statistics as st
import sys
from collections import Counter
from pathlib import Path

from inspect_ai.log import read_eval_log


def _findings(score) -> dict[str, float] | None:
    grade = (score.metadata or {}).get("grade") if score else None
    if not grade:
        return None
    return {k: v["score"] for k, v in grade.get("scores", {}).items() if "score" in v}


def _metrics(findings: dict[str, float] | None, tldr) -> dict | None:
    if not findings or tldr is None or not isinstance(tldr.value, (int, float)):
        return None
    cov = st.mean(max(2 * s - 1, 0) for s in findings.values())
    return {"raw": st.mean(findings.values()), "cov": cov, "tldr": float(tldr.value),
            "comb": 0.7 * cov + 0.3 * float(tldr.value)}


def main() -> None:
    paths = [p for a in sys.argv[1:] for p in (sorted(Path(a).glob("*.eval")) if Path(a).is_dir() else [Path(a)])]
    rows: dict[str, list[dict]] = {}
    moves: dict[str, Counter] = {}
    for path in paths:
        log = read_eval_log(str(path))
        for s in log.samples or []:
            scores = s.scores or {}
            draft_f = _findings(scores.get("sheet_scorer"))
            draft = _metrics(draft_f, scores.get("sheet_scorer1"))
            print(f"\n{path.name} epoch {s.epoch}")
            if draft:
                rows.setdefault("draft", []).append(draft)
                print(f"  {'draft':16} raw {draft['raw']:.3f} cov {draft['cov']:.3f} tldr {draft['tldr']:.2f} "
                      f"comb {draft['comb']:.3f}")
            for name, meta in (s.metadata.get("writer_variants") or {}).items():
                key = name.replace("-", "_")
                vf = _findings(scores.get(f"v2_{key}"))
                m = _metrics(vf, scores.get(f"tldrh_{key}"))
                cost = (f"{meta.get('status')}, {meta.get('calls')} calls, {meta.get('output_tokens', 0):,} out, "
                        f"{meta.get('seconds')}s")
                if not m:
                    print(f"  {name:16} not graded ({cost})")
                    continue
                rows.setdefault(name, []).append(m)
                print(f"  {name:16} raw {m['raw']:.3f} cov {m['cov']:.3f} tldr {m['tldr']:.2f} comb {m['comb']:.3f}"
                      f"  ({cost})")
                if draft_f and vf:
                    c = moves.setdefault(name, Counter())
                    for f, d in draft_f.items():
                        if f in vf and vf[f] > d:
                            c[(f, "+")] += 1
                        elif f in vf and vf[f] < d:
                            c[(f, "-")] += 1
    print("\nMeans (n samples):")
    for name, ms in rows.items():
        print(f"  {name:16} n={len(ms)} raw {st.mean(m['raw'] for m in ms):.3f} cov {st.mean(m['cov'] for m in ms):.3f} "
              f"tldr {st.mean(m['tldr'] for m in ms):.2f} comb {st.mean(m['comb'] for m in ms):.3f}")
    for name, c in moves.items():
        gains = ", ".join(f"{f}×{n}" for (f, d), n in sorted(c.items()) if d == "+")
        losses = ", ".join(f"{f}×{n}" for (f, d), n in sorted(c.items()) if d == "-")
        print(f"\n{name} against the draft: gained {gains or 'none'}; lost {losses or 'none'}")


if __name__ == "__main__":
    main()
