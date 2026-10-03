"""Themes: what is typical. Topics shared by many records and actors, found from topic words
(words that appear in some but not most records), not from copy-paste similarity.

Near-duplicate clusters capture records that were copied; they miss an activity that many
actors describe in their own words (hundreds of different posts about the same thing). A
theme groups clusters whose leaders share a characteristic word, ranked by how many
distinct actors take part. The same topic-word sets also give every record a "related"
count: how many other records share most of its topic words, which separates an unusual
variant of a widespread activity from an isolated oddity.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .cluster import Cluster

_WORDS = re.compile(r"[^\W_]{3,}", re.UNICODE)
_HEXISH = re.compile(r"^[0-9a-f]+$")
# Common English function words; other languages' function words usually exceed the
# document-frequency cap on their own.
STOPWORDS = frozenset("""
the and for are but not you all any can had her was one our out has have him his how its may new now old
see two way who did get got let put say she too use this that with from they will would there their what
about which when were been into more some than then them these also just only very your here over such
after before should could where while being each other most because does doing done both same under again
further once off own why let lets via per yes yet within without upon onto ever every much many make made
""".split())
MIN_LEADERS = 100  # fields with fewer clusters get no themes
MAX_THEMES = 12
RELATED_OVERLAP = 0.5  # share of an item's topic words another record must have to count as related


@dataclass
class Theme:
    tid: str
    table: str
    field: str
    seed: str
    words: list[str]
    clusters: list[str]  # cluster ids
    rows: int = 0
    actors: int = 0
    example: str = ""  # cluster id of the most typical member


@dataclass
class TopicIndex:
    words: dict[str, frozenset[str]] = field(default_factory=dict)  # cid -> topic words
    inv: dict[str, list[str]] = field(default_factory=dict)  # topic word -> cids
    themes: list[Theme] = field(default_factory=list)


def _is_word(w: str) -> bool:
    return w not in STOPWORDS and any(c.isalpha() for c in w) and not _HEXISH.match(w) and sum(c.isdigit() for c in w) <= len(w) // 3


def build_topics(idx, clusters: list[Cluster], table: str, fname: str, next_tid: int) -> TopicIndex:
    raw = {c.cid: frozenset(w for w in (x.lower() for x in _WORDS.findall(idx.display_text(c, c.leader)[:3000]))
                            if _is_word(w)) for c in clusters}
    n = len(raw)
    df: Counter = Counter()
    for ws in raw.values():
        df.update(ws)
    hi = max(2, int(0.15 * n))  # topic words for "related": specific enough to mean the same thing
    theme_hi = max(2, int(0.40 * n))  # theme seeds may be broader: a dominant activity is one theme
    ti = TopicIndex()
    for cid, ws in raw.items():
        topic = frozenset(w for w in ws if 2 <= df[w] <= hi)
        ti.words[cid] = topic
        for w in topic:
            ti.inv.setdefault(w, []).append(cid)
    if n < MIN_LEADERS:
        return ti
    by_id = {c.cid: c for c in clusters}
    actor_field = idx.profiles[table].actor_fields[0] if idx.profiles[table].actor_fields else None

    def actors_of(cids) -> set:
        out = set()
        if not actor_field:
            return out
        rows = idx.tables[table].rows
        for cid in cids:
            for r in by_id[cid].members:
                v = rows[r].get(actor_field)
                if v not in (None, ""):
                    out.add(v)
        return out

    lo = max(5, int(0.003 * n))
    inv_all: dict[str, list[str]] = defaultdict(list)
    for cid, ws in raw.items():
        for w in ws:
            if lo <= df[w] <= theme_hi:
                inv_all[w].append(cid)
    cands = list(inv_all)
    breadth = {w: len(actors_of(inv_all[w])) or len(inv_all[w]) for w in cands}
    absorbed: set[str] = set()
    for w in sorted(cands, key=lambda w: (-breadth[w], w)):
        if len(ti.themes) >= MAX_THEMES:
            break
        if w in absorbed:
            continue
        members = set(inv_all[w])
        if len(members) < lo:
            continue
        # A candidate mostly covered by an earlier theme is the same topic: fold it in.
        overlap = max((len(members & set(t.clusters)) / len(members) for t in ti.themes), default=0.0)
        if overlap >= 0.4:
            absorbed.add(w)
            continue
        # Characteristic words: frequent inside the theme, rare outside it.
        inside: Counter = Counter()
        for cid in members:
            inside.update(x for x in raw[cid] if x in inv_all)
        scored = []
        for x, k in inside.items():
            if k < 0.2 * len(members):
                continue
            lift = (k / len(members)) / (df[x] / n)
            scored.append((k * lift, x))
        words = [w] + [x for _, x in sorted(scored, reverse=True) if x != w][:6]
        # Words whose records mostly fall inside this theme belong to it, not to a new one.
        for x, _k in inside.items():
            docs = inv_all.get(x, ())
            if docs and sum(1 for d in docs if d in members) >= 0.6 * len(docs):
                absorbed.add(x)
        example = max(members, key=lambda cid: (len(raw[cid] & set(words)), -by_id[cid].leader))
        theme = Theme(f"t{next_tid + len(ti.themes)}", table, fname, w, words,
                      sorted(members, key=lambda cid: by_id[cid].leader),
                      rows=sum(by_id[cid].size for cid in members),
                      actors=len(actors_of(members)), example=example)
        ti.themes.append(theme)
    return ti


def related(ti: TopicIndex, cid: str) -> tuple[int, list[str]]:
    """How many other records share at least half of this record's topic words, and the
    words they most often share."""
    mine = ti.words.get(cid)
    if not mine:
        return 0, []
    hits: Counter = Counter()
    for w in mine:
        for other in ti.inv.get(w, ()):
            if other != cid:
                hits[other] += 1
    need = max(2, int(RELATED_OVERLAP * len(mine) + 0.999))
    rel = [o for o, k in hits.items() if k >= need]
    shared: Counter = Counter()
    for o in rel:
        shared.update(ti.words[o] & mine)
    return len(rel), [w for w, _ in shared.most_common(4)]
