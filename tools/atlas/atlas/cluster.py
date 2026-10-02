"""Group near-duplicate values of one text field.

Short values (a few tokens) go through a small Drain-style template miner: values with
the same token count and similar tokens share a template, with differing positions
replaced by ``<*>``. Long values go through one-permutation MinHash over word 4-shingles
with LSH banding, and leader clustering: a value joins the most similar existing leader
above the threshold, otherwise it becomes a leader. Leaders are compared, members are
not, so a chain of gradually drifting variants cannot merge into one cluster.

Pure standard library: the sandbox image has no third-party packages.
"""

from __future__ import annotations

import re
import zlib
from collections import defaultdict
from dataclasses import dataclass, field

SHORT_TOKENS = 12  # values with at most this many tokens are templated, not MinHashed
SHINGLE = 4
K = 64  # MinHash bins
BANDS, ROWS = 16, 4  # LSH: candidate pairs from ~0.5 Jaccard upward
LONG_THRESHOLD = 0.5  # estimated Jaccard to join a leader
SHORT_THRESHOLD = 0.5  # share of matching non-wildcard positions to join a template

_BIN_SHIFT = 58  # top 6 bits pick one of 64 bins
_LOW = (1 << _BIN_SHIFT) - 1
_HEX = re.compile(r"\b[0-9a-f]{8,}\b")
_NUM = re.compile(r"\d+")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_SHORT_TOKEN = re.compile(r"[^\s/?&=,;:|]+")


@dataclass
class Cluster:
    table: str
    field: str
    members: list[int] = field(default_factory=list)  # row indices into the table
    template: str | None = None  # short-value clusters only
    cid: str = ""
    kind: str = "cluster"  # or "window": consecutive rows of a low-redundancy table
    signals: set = field(default_factory=set)
    score: float = 0.0

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def leader(self) -> int:
        return self.members[0]


def normalize(text: str) -> str:
    return _NUM.sub("0", _HEX.sub("H", text.lower()))


def _signature(tokens: list[str]) -> tuple[int, ...] | None:
    if len(tokens) < SHINGLE:
        shingles = {" ".join(tokens)}
    else:
        shingles = {" ".join(tokens[i : i + SHINGLE]) for i in range(len(tokens) - SHINGLE + 1)}
    bins = [_LOW + 1] * K
    for s in shingles:
        e = s.encode()
        h = (zlib.crc32(e) << 32) | zlib.crc32(e, 0x5BD1E995)
        b = h >> _BIN_SHIFT
        v = h & _LOW
        if v < bins[b]:
            bins[b] = v
    # Densify empty bins from the next filled bin (rotation), so short texts still compare.
    filled = [i for i in range(K) if bins[i] <= _LOW]
    if not filled:
        return None
    if len(filled) < K:
        out = list(bins)
        for i in range(K):
            if out[i] > _LOW:
                j = next((f for f in filled if f > i), filled[0])
                out[i] = bins[j] ^ (i * 0x9E3779B97F4A7C15 & _LOW)
        bins = out
    return tuple(bins)


def _similarity(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    return sum(1 for x, y in zip(a, b) if x == y) / K


def _short_tokens(text: str) -> list[str]:
    toks = _SHORT_TOKEN.findall(text)
    return ["<*>" if any(c.isdigit() for c in t) else t for t in toks]


def _cluster_short(items: list[tuple[int, str]]) -> list[tuple[str, list[int]]]:
    groups: dict[tuple, list[list]] = defaultdict(list)  # key -> [[template_tokens, members]]
    for idx, text in items:
        toks = _short_tokens(text)
        first = toks[0].lower() if toks and toks[0] != "<*>" else "<*>"
        cands = groups[(len(toks), first)]
        best, best_sim = None, -1.0
        for c in cands:
            tpl = c[0]
            fixed = [i for i, t in enumerate(tpl) if t != "<*>"]
            if not fixed:
                sim = 1.0
            else:
                sim = sum(1 for i in fixed if tpl[i].lower() == toks[i].lower()) / len(fixed)
            if sim > best_sim:
                best, best_sim = c, sim
        if best is not None and best_sim >= SHORT_THRESHOLD:
            best[0] = [a if a.lower() == b.lower() else "<*>" for a, b in zip(best[0], toks)]
            best[1].append(idx)
        else:
            cands.append([toks, [idx]])
    out = []
    for cands in groups.values():
        for tpl, members in cands:
            out.append((" ".join(tpl) if tpl else "(empty)", members))
    return out


def _cluster_long(items: list[tuple[int, str]]) -> list[list[int]]:
    exact: dict[str, int] = {}  # normalized text -> leader position in `leaders`
    leaders: list[tuple[int, tuple]] = []  # (row idx, signature)
    members: list[list[int]] = []
    buckets: dict[tuple, list[int]] = defaultdict(list)
    for idx, text in items:
        norm = normalize(text)
        pos = exact.get(norm)
        if pos is not None:
            members[pos].append(idx)
            continue
        sig = _signature(_WORD.findall(norm))
        if sig is None:
            pos = len(members)
            members.append([idx])
            leaders.append((idx, ()))
            exact[norm] = pos
            continue
        keys = [(b, sig[b * ROWS : (b + 1) * ROWS]) for b in range(BANDS)]
        cands = {p for k in keys for p in buckets.get(k, ())}
        best, best_sim = None, 0.0
        for p in cands:
            s = _similarity(sig, leaders[p][1])
            if s > best_sim:
                best, best_sim = p, s
        if best is not None and best_sim >= LONG_THRESHOLD:
            members[best].append(idx)
            exact[norm] = best
            continue
        pos = len(members)
        members.append([idx])
        leaders.append((idx, sig))
        exact[norm] = pos
        for k in keys:
            buckets[k].append(pos)
    return members


def cluster_field(table: str, fname: str, rows: list[dict], order: list[int]) -> list[Cluster]:
    """Cluster one field. ``order`` is the row order to process (time order when known),
    so each cluster's first member, its leader, is its earliest occurrence."""
    short, long = [], []
    for i in order:
        v = rows[i].get(fname)
        if v is None or v == "" or v == []:
            continue
        text = v if isinstance(v, str) else str(v)
        (short if len(_SHORT_TOKEN.findall(text)) <= SHORT_TOKENS else long).append((i, text))
    clusters = [Cluster(table, fname, m, tpl) for tpl, m in _cluster_short(short)]
    clusters += [Cluster(table, fname, m) for m in _cluster_long(long)]
    return clusters
