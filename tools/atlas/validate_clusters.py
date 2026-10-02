#!/usr/bin/env python3
"""Generic clustering diagnostics for one data directory (no model calls, no labels).

    python3 tools/atlas/validate_clusters.py data/verbatim [--seed 0]

Per clustered field it prints:
  * the cluster-size distribution and the share of values that are singletons;
  * homogeneity: exact word-4-shingle Jaccard between random member pairs of big clusters
    (low values mean unrelated values were merged);
  * fragmentation: for sampled singletons, the best exact Jaccard to any other leader
    (high values mean near-duplicates were left apart).
Thresholds are tuned only on these numbers, never on where a known finding lands.
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atlas import cluster as C  # noqa: E402
from atlas.index import build_index  # noqa: E402

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def shingles(text: str) -> set[str]:
    toks = _WORD.findall(C.normalize(text))
    if len(toks) < C.SHINGLE:
        return {" ".join(toks)}
    return {" ".join(toks[i : i + C.SHINGLE]) for i in range(len(toks) - C.SHINGLE + 1)}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a or b else 1.0


def bucket(n: int) -> str:
    for hi, name in ((1, "1"), (2, "2"), (5, "3-5"), (20, "6-20"), (100, "21-100")):
        if n <= hi:
            return name
    return ">100"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--samples", type=int, default=200)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    t0 = time.time()
    idx = build_index(Path(args.data_dir), use_cache=False)
    print(f"index built in {time.time() - t0:.1f}s; {len(idx.clusters)} clusters")
    by_field: dict[tuple, list] = {}
    for c in idx.clusters:
        by_field.setdefault((c.table, c.field), []).append(c)
    for (table, fname), cl in by_field.items():
        rows = idx.tables[table].rows
        text = lambda i: str(rows[i].get(fname))  # noqa: E731
        n_vals = sum(c.size for c in cl)
        dist = Counter(bucket(c.size) for c in cl)
        singles = dist.get("1", 0)
        short = sum(1 for c in cl if c.template is not None)
        print(f"\n== {table}.{fname}: {n_vals} values -> {len(cl)} clusters "
              f"({short} templated); singletons {singles} ({singles / n_vals:.1%} of values)")
        print("   sizes: " + "  ".join(f"{k}:{dist.get(k, 0)}" for k in ("1", "2", "3-5", "6-20", "21-100", ">100")))
        # Homogeneity of big clusters, long values only (templates are homogeneous by construction).
        big = [c for c in cl if c.size >= 6 and c.template is None]
        sims = []
        for c in rng.sample(big, min(len(big), 40)):
            for _ in range(5):
                a, b = rng.sample(c.members, 2)
                sims.append(jaccard(shingles(text(a)), shingles(text(b))))
        if sims:
            sims.sort()
            print(f"   homogeneity (member-pair Jaccard, {len(sims)} pairs): "
                  f"p10 {sims[len(sims) // 10]:.2f}  median {sims[len(sims) // 2]:.2f}  "
                  f"share<0.2 {sum(s < 0.2 for s in sims) / len(sims):.1%}")
        # Fragmentation: singleton vs all other long leaders.
        long_cl = [c for c in cl if c.template is None]
        lone = [c for c in long_cl if c.size == 1]
        if lone and len(long_cl) > 1:
            leader_sh = {id(c): shingles(text(c.leader)) for c in long_cl}
            best = []
            for c in rng.sample(lone, min(len(lone), args.samples)):
                s = leader_sh[id(c)]
                best.append(max(jaccard(s, leader_sh[id(o)]) for o in long_cl if o is not c))
            best.sort()
            print(f"   fragmentation (singleton best-leader Jaccard, {len(best)} sampled): "
                  f"median {best[len(best) // 2]:.2f}  p90 {best[int(len(best) * 0.9)]:.2f}  "
                  f"share>=0.5 {sum(b >= 0.5 for b in best) / len(best):.1%}  share>=0.7 {sum(b >= 0.7 for b in best) / len(best):.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
