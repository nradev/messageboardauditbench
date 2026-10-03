#!/usr/bin/env python3
"""How agents used atlas in Inspect logs: one block per sample.

    uv run python tools/atlas/usage_metrics.py logs/atlas-smoke/*.eval [--data data/verbatim]

Per sample: tool calls by function, atlas calls by subcommand and their share of all calls,
when atlas was first and last used (seconds after the first tool call, and as a share of the
span of tool activity), how often and when `unseen` ran, coverage of the most salient small
clusters (needs --data, to rebuild the index), report words and any scores. Host-side only:
it imports inspect_ai, so it is not copied into the sandbox.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from collections import Counter
from pathlib import Path

from inspect_ai.log import read_eval_log

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _resolve(value, attachments: dict) -> str:
    """Inspect stores long tool arguments as ``attachment://<hash>`` references."""
    v = str(value)
    if v.startswith("attachment://"):
        return str(attachments.get(v[len("attachment://"):], ""))
    return v


def _atlas_subcommand(event, attachments: dict | None = None) -> str:
    args = {k: _resolve(v, attachments or {}) for k, v in (event.arguments or {}).items()}
    if event.function == "atlas":
        cmd = args.get("command", "")
    else:  # atlas run from bash
        cmd = str(args.get("command", ""))
        if "atlas " not in cmd:
            return ""
        cmd = cmd[cmd.index("atlas ") + len("atlas "):]
    try:
        parts = [p for p in shlex.split(cmd) if not p.startswith("-")]
    except ValueError:
        parts = cmd.split()
    return parts[0] if parts else "(none)"


def _headline(scores) -> str:
    """The published metric: finding coverage = mean of max(2s - 1, 0) over findings,
    combined = 0.7 x coverage + 0.3 x TL;DR (see README "How grading works")."""
    cov = tldr = raw = None
    for sc in scores.values():
        grade = (sc.metadata or {}).get("grade") or {}
        items = grade.get("scores") or {}
        if grade.get("rubric") == "v2" and items:
            cov = sum(max(2 * v["score"] - 1, 0) for v in items.values()) / len(items)
            raw = sum(v["score"] for v in items.values()) / len(items)
        elif grade.get("rubric") == "tldrh" and items:
            tldr = sum(v["score"] for v in items.values()) / len(items)
    if cov is None or tldr is None:
        return ""
    return (f"raw findings {raw:.3f}, coverage {cov:.3f}, tldr {tldr:.2f}, "
            f"combined {0.7 * cov + 0.3 * tldr:.3f}")


def summarize(path: Path, data: Path | None) -> list[str]:
    log = read_eval_log(str(path))
    out = [f"== {path.name}  status={log.status}"]
    idx = None
    if data:
        from atlas.index import build_index

        idx = build_index(data)
    for s in log.samples or []:
        tools = [e for e in s.events if e.event == "tool"]
        if not tools:
            out.append(f"  {s.id}: no tool calls")
            continue
        t0 = tools[0].timestamp
        end = tools[-1].timestamp
        span = max((end - t0).total_seconds(), 1.0)
        by_fn = Counter(e.function for e in tools)
        atlas_calls = [(e, _atlas_subcommand(e, s.attachments)) for e in tools]
        atlas_calls = [(e, sub) for e, sub in atlas_calls if e.function == "atlas" or sub]
        subs = Counter(sub for _, sub in atlas_calls)
        rel = [(e.timestamp - t0).total_seconds() for e, _ in atlas_calls]
        unseen = [(e.timestamp - t0).total_seconds() for e, sub in atlas_calls if sub == "unseen"]
        out.append(f"  sample {s.id}")
        out.append(f"    tool calls {len(tools)}: " + ", ".join(f"{f} {n}" for f, n in by_fn.most_common()))
        out.append(f"    atlas calls {len(atlas_calls)} ({len(atlas_calls) / len(tools):.0%} of all): "
                   + ", ".join(f"{k} {n}" for k, n in subs.most_common()))
        if rel:
            out.append(f"    atlas first {rel[0]:.0f}s, last {rel[-1]:.0f}s of {span:.0f}s tool activity "
                       f"(last at {rel[-1] / span:.0%})")
        out.append(f"    unseen calls {len(unseen)}" + (f", last at {unseen[-1]:.0f}s ({unseen[-1] / span:.0%})"
                                                         if unseen else ""))
        cov = s.metadata.get("atlas_coverage") or []
        opened = {cid for c in cov for cid in c.get("opened", [])}
        listed = {cid for c in cov if isinstance(c.get("listed"), list) for cid in c["listed"]}
        out.append(f"    opened {len(opened)} clusters/themes/windows; shown in listings {len(listed)}; "
                   f"rows seen in full {len({r for c in cov for r in c.get('seen_rows', [])})}")
        if idx is not None:
            top = set(idx.top_salient)
            out.append(f"    most salient rare records: shown {len(listed & top)}/{len(top)}, opened {len(opened & top)}/{len(top)}"
                       + ("" if any(isinstance(c.get("listed"), list) for c in cov) else " (log predates shown-tracking)"))
        words = s.metadata.get("report_words")
        if words is not None:
            out.append(f"    report words {words}")
        if s.scores:
            out.append("    scores: " + ", ".join(f"{k}={v.value}" for k, v in s.scores.items()))
            headline = _headline(s.scores)
            if headline:
                out.append("    headline: " + headline)
        if s.metadata.get("minimum_runtime_violation"):
            out.append("    minimum-runtime violation: report accepted at the early-completion cap")
        if s.error:
            out.append(f"    error: {str(s.error.message)[:200]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--data", help="data directory the runs used, to measure coverage of the top salient set")
    args = ap.parse_args()
    for p in args.logs:
        print("\n".join(summarize(Path(p), Path(args.data) if args.data else None)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
