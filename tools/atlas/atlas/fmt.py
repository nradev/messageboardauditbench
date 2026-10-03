"""Shared output helpers: snippets, times, row references, footers, pagination, diversity."""

from __future__ import annotations

import re

from .cluster import Cluster
from .index import Index

PAGE = 15
SNIP = 160
_WORDS = re.compile(r"[^\W_]{3,}", re.UNICODE)


def snip(text: str, n: int = SNIP) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def fmt_time(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "-"


def short_day(dt) -> str:
    return dt.strftime("%m-%d") if dt else "?"


def ref(table: str, row: int) -> str:
    """``table:line``: the 1-based line number in the source file (also valid in shell/python)."""
    return f"{table}:{row + 1}"


def ref_id(idx: Index, table: str, row: int) -> str:
    """The ref plus the record's own id field, when the file has one, for citing."""
    nid = idx.native_id(table, row)
    return f"{ref(table, row)} ({idx.profiles[table].id_field}={nid})" if nid else ref(table, row)


def footer(*cmds: str) -> str:
    return "→ next: " + " · ".join(c for c in cmds if c)


def paginate(items: list, page: int, size: int = PAGE) -> tuple[list, str]:
    total = len(items)
    start = (page - 1) * size
    shown = items[start : start + size]
    if total > start + len(shown):
        note = f"(showing {start + 1}-{start + len(shown)} of {total}; --page {page + 1} for more)"
    elif total:
        note = f"(showing {start + 1}-{start + len(shown)} of {total})"
    else:
        note = "(none)"
    return shown, note


def _words(idx: Index, c: Cluster) -> frozenset[str]:
    return frozenset(w.lower() for w in _WORDS.findall(idx.display_text(c, c.leader)[:3000]))


def _keywords(sets: list[frozenset[str]]) -> list[frozenset[str]]:
    """Each item's topic words: the ones it shares with only a few other items of the pool
    (document frequency 2..15% of the pool). Two items about the same thing share many of
    these; unrelated items share only common words, which are excluded."""
    df: dict[str, int] = {}
    for s in sets:
        for w in s:
            df[w] = df.get(w, 0) + 1
    hi = max(2, int(0.15 * len(sets)))
    out = []
    for s in sets:
        out.append(frozenset(w for w in s if 2 <= df[w] <= hi))
    return out


def diversify(idx: Index, ranked: list[Cluster], pool: int = 300, penalty: float = 0.7) -> list[Cluster]:
    """Maximal marginal relevance over the top ``pool`` items of a ranked list: each next item
    maximises (score / top score) - penalty × (max topic-word overlap with the items already
    picked), so a page shows different kinds of record rather than variants of one topic.
    Items beyond the pool keep their order."""
    head, tail = ranked[:pool], ranked[pool:]
    if len(head) < 3:
        return ranked
    top = max(c.score for c in head) or 1.0
    keys = dict(zip((c.cid for c in head), _keywords([_words(idx, c) for c in head]), strict=True))
    picked: list[Cluster] = []
    best_sim = {c.cid: 0.0 for c in head}
    remaining = list(head)
    while remaining:
        choice = max(remaining, key=lambda c: c.score / top - penalty * best_sim[c.cid])
        picked.append(choice)
        remaining.remove(choice)
        w = keys[choice.cid]
        for c in remaining:
            o = keys[c.cid]
            if w and o:
                j = len(w & o) / min(len(w), len(o))  # overlap coefficient
                if j > best_sim[c.cid]:
                    best_sim[c.cid] = j
    return picked + tail
