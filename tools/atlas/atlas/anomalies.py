"""Anomalies: a few generic detectors, each returning a short ranked list (one example and a
count per item), so the output stays readable on any corpus.

  look-alike identifiers  identifier-like values (short values of any non-text field, and
                          extracted hosts, e-mails, paths)
                          that differ from another value only by look-alike characters:
                          the Unicode TR39 confusable skeleton (vendored table) is the same,
                          the raw values are not (e.g. a Cyrillic "е" in place of a Latin "e")
  mixed-script tokens     words mixing letters of several scripts, anywhere in the text;
                          those whose skeleton is a word used elsewhere in plain ASCII first
  actor bursts            an actor value with far more rows in one time bucket (the corpus's
                          time unit) than its usual share of that bucket's activity predicts
  record bursts           many copies of one repeated record within a single hour
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict

from . import coverage
from .confusables import CONFUSABLES
from .fmt import fmt_time, footer, ref, short_day, snip
from .index import Index
from .signals import scripts_of

MAX_ITEMS = 8
_NON_ASCII_WORD = re.compile(r"\w*[^\x00-\x7f]\w*")
_WORD = re.compile(r"[A-Za-z0-9_]{3,}")
_MAX_ID = 80  # longer values are text, not identifiers


def skeleton(text: str) -> str:
    """Confusable skeleton: NFKC, look-alike characters mapped to the ASCII they imitate,
    lower case. Two strings with the same skeleton look alike."""
    t = unicodedata.normalize("NFKC", text)
    return "".join(CONFUSABLES.get(ord(c), c) for c in t).lower()


def _char_names(raw: str, other: str) -> str:
    """The characters of ``raw`` that are look-alikes, e.g. 'U+0435 CYRILLIC SMALL LETTER IE for "e"'."""
    out = []
    for c in dict.fromkeys(raw):
        if ord(c) >= 128 and ord(c) in CONFUSABLES:
            try:
                name = unicodedata.name(c)
            except ValueError:
                name = "?"
            out.append(f'U+{ord(c):04X} {name} for "{CONFUSABLES[ord(c)]}"')
    return "; ".join(out[:3])


def _identifier_values(idx: Index) -> dict[tuple[str, str], dict]:
    """Every short string value of every non-text field (near-unique fields included, since
    an imitated name may occur only once), with its row count, first row and span."""
    vals: dict[tuple[str, str], dict] = {}
    for table, t in idx.tables.items():
        p = idx.profiles[table]
        skip = set(p.text_fields) | {p.time_field}
        for i, r in enumerate(t.rows):
            for f, v in r.items():
                if f in skip:
                    continue
                for x in (v if isinstance(v, list) else [v]):
                    if isinstance(x, str) and 3 <= len(x) <= _MAX_ID:
                        e = vals.get((f"{table}.{f}", x))
                        if e is None:
                            vals[(f"{table}.{f}", x)] = {"rows": 1, "first": (table, i)}
                        else:
                            e["rows"] += 1
    for (kind, value), stat in idx.entities.items():  # extracted hosts, e-mails, paths
        if kind in ("host", "email", "path") and isinstance(value, str):
            vals.setdefault((kind, value), {"rows": stat.rows, "first": stat.first_ref})
    return vals


def lookalike_groups(idx: Index) -> list[dict]:
    by_skel: dict[str, list[tuple[str, str, dict]]] = defaultdict(list)
    for (kind, value), stat in _identifier_values(idx).items():
        if any(ord(c) >= 128 for c in value) or value.isascii():
            by_skel[skeleton(value)].append((kind, value, stat))
    groups = []
    for _sk, members in by_skel.items():
        raws = {v for _, v, _ in members}
        if len({r.lower() for r in raws}) < 2 or not any(ord(c) >= 128 for r in raws for c in r):
            continue  # case-only variants, or no look-alike character involved
        plain = sorted((m for m in members if m[1].isascii()), key=lambda m: -m[2]["rows"])
        odd = sorted((m for m in members if not m[1].isascii()), key=lambda m: -m[2]["rows"])
        if not plain:
            continue  # look-alikes of each other only, with no plain original in the corpus
        main = plain[0]
        # Most notable: an established plain value imitated by a rarer look-alike.
        score = (main[2]["rows"] + 1) / (min(m[2]["rows"] for m in odd) + 1)
        groups.append({"main": main, "odd": odd, "score": score})
    groups.sort(key=lambda g: -g["score"])
    return groups


def mixed_script_tokens(idx: Index) -> list[dict]:
    rows: dict[str, list[tuple[str, int]]] = defaultdict(list)
    ascii_words: set[str] = set()
    for table, t in idx.tables.items():
        for i, r in enumerate(t.rows):
            for v in r.values():
                if not isinstance(v, str):
                    continue
                for tok in _NON_ASCII_WORD.findall(v):
                    if len(tok) >= 3 and len(scripts_of(tok)) > 1:
                        hits = rows[tok]
                        if not hits or hits[-1] != (table, i):
                            hits.append((table, i))
    if not rows:
        return []
    for t in idx.tables.values():
        for r in t.rows:
            for v in r.values():
                if isinstance(v, str):
                    ascii_words.update(w.lower() for w in _WORD.findall(v[:5000]))
    out = []
    for tok, hits in rows.items():
        disguised = skeleton(tok) in ascii_words
        out.append({"token": tok, "rows": hits, "disguised": disguised})
    out.sort(key=lambda x: (not x["disguised"], len(x["rows"]), x["token"]))
    return out


def actor_bursts(idx: Index) -> list[dict]:
    """Actor values far busier in one time bucket (the corpus's time unit, e.g. a day or 10
    minutes) than their usual share of that bucket's activity predicts (so a corpus-wide busy
    period does not make every actor look bursty)."""
    from .timeline import time_unit

    unit = time_unit(idx)
    out = []
    for table, p in idx.profiles.items():
        if not p.time_field:
            continue
        rows = idx.tables[table].rows
        day_total: Counter = Counter()
        times = [idx.time_of(table, i) for i in range(len(rows))]
        for t in times:
            if t:
                day_total[unit.floor(t)] += 1
        for f in p.actor_fields[:2]:
            per: dict[str, Counter] = defaultdict(Counter)
            first: dict[tuple[str, object], int] = {}
            for i, r in enumerate(rows):
                v = r.get(f)
                if v in (None, "") or not times[i]:
                    continue
                d = unit.floor(times[i])
                per[str(v)][d] += 1
                first.setdefault((str(v), d), i)
            for v, days in per.items():
                if len(days) < 3:
                    continue
                # Typical share of a day's rows: the median over the actor's active days, so the
                # burst itself does not inflate the baseline.
                shares = sorted(k / day_total[d] for d, k in days.items())
                typical = shares[len(shares) // 2]
                day, n, expected = max(((d, k, typical * day_total[d]) for d, k in days.items()),
                                       key=lambda x: x[1] - x[2])
                if n >= 20 and n >= 3 * max(expected, 1):
                    out.append({"table": table, "field": f, "value": v, "day": day, "unit": unit, "count": n,
                                "expected": expected, "active": len(days), "row": first[(v, day)],
                                "excess": n - expected})
    out.sort(key=lambda b: -b["excess"])
    return out


def record_bursts(idx: Index) -> list[dict]:
    """Copies of one repeated record within a single hour. The window stays an hour at every
    corpus scale: finer windows make the absolute threshold too strict for low-volume
    corpora, and coarser ones blur floods into ordinary activity."""
    from .timeline import Unit

    window = Unit("hour", 3600)
    out = []
    for c in idx.clusters:
        if c.size < 20 or c.kind != "cluster":
            continue
        hours = Counter()
        first = {}
        for r in c.members:
            t = idx.time_of(c.table, r)
            if t:
                h = window.floor(t)
                hours[h] += 1
                first.setdefault(h, r)
        if not hours or len(hours) < 2:
            continue
        h, n = hours.most_common(1)[0]
        if n >= max(20, 0.5 * c.size):
            out.append({"cluster": c, "hour": h, "window": window, "count": n, "row": first[h]})
    out.sort(key=lambda b: -b["count"])
    return out


def _when(idx: Index, first) -> str:
    if not first:
        return "?"
    t = idx.time_of(*first)
    return f"{ref(*first)}" + (f" {short_day(t)}" if t else "")


def cmd_anomalies(idx: Index, args) -> str:
    out = ["Anomalies (generic detectors; each list most notable first, capped):"]
    listed: list[str] = []

    groups = lookalike_groups(idx)
    out.append(f"\nLook-alike identifiers ({len(groups)} groups: values that differ only by look-alike characters):")
    if not groups:
        out.append("  none")
    for g in groups[:MAX_ITEMS]:
        kind, value, stat = g["main"]
        line = f"  {value!r} ({kind}, {stat['rows']:,} rows, first {_when(idx, stat['first'])})"
        for k2, v2, s2 in g["odd"][:3]:
            line += (f"\n     look-alike {v2!r} ({k2}, {s2['rows']:,} rows, first {_when(idx, s2['first'])}): "
                     f"{_char_names(v2, value)}")
        out.append(line)

    toks = mixed_script_tokens(idx)
    out.append(f"\nMixed-script tokens ({len(toks)} distinct; words imitating a plain-ASCII word in the corpus first):")
    if not toks:
        out.append("  none")
    for x in toks[:MAX_ITEMS]:
        t, r = x["rows"][0]
        scripts = ", ".join(sorted(scripts_of(x["token"])))
        tag = f" looks like {skeleton(x['token'])!r}" if x["disguised"] else ""
        out.append(f"  {x['token']!r} ({scripts}){tag}: {len(x['rows'])} rows, e.g. {ref(t, r)}")

    bursts = actor_bursts(idx)
    out.append(f"\nActor bursts ({len(bursts)}: an actor far busier in one time bucket than its usual share predicts):")
    if not bursts:
        out.append("  none")
    for b in bursts[:MAX_ITEMS]:
        u = b["unit"]
        out.append(f"  {b['table']}.{b['field']}={snip(b['value'], 40)}: {b['count']:,} rows "
                   f"{'in the ' + u.name + ' from' if u.sub_day else 'on'} {u.label(b['day'])} "
                   f"(about {b['expected']:.0f} expected from its usual share; {b['active']} active {u.plural}), "
                   f"first {ref(b['table'], b['row'])}")

    rb = record_bursts(idx)
    out.append(f"\nRecord bursts ({len(rb)}: copies of one repeated record within a single hour):")
    if not rb:
        out.append("  none")
    for b in rb[:MAX_ITEMS]:
        c = b["cluster"]
        out.append(f"  {c.cid} ×{c.size}: {b['count']:,} in the {b['window'].name} from {fmt_time(b['hour'])}, "
                   f"first {ref(c.table, b['row'])} | {snip(idx.display_text(c, c.leader), 80)}")
        listed.append(c.cid)

    out.append(footer("atlas pivot VALUE --exact", "atlas show REF", "atlas expand cNN"))
    coverage.record("anomalies", [], listed=listed)
    return "\n".join(out)
