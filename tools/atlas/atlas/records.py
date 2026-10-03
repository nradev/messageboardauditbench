"""Resolve a set of records into JSON for readers (the reading crew).

A SET is one of:
  cNN | tNN | wNN        a cluster, theme or window
  grep:REGEX             rows with a match in any field (smart case)
  pivot:VALUE            rows containing VALUE in any field (case-insensitive substring)
  rows:TABLE             rows of one file, filtered with --where (same syntax as `atlas rows`)
  around:REF             rows next to REF in the same file, in time order (--n per side)
  refs:REF,REF,...       exactly these rows (file:line or record ids)
  sweep                  the corpus's most informative records the agent has not read,
                         opened or been shown in a listing: mostly salient rare records
                         (diverse), a few theme examples and repeated records, and (for
                         low-redundancy files) the most salient windows; each record says
                         why it was picked. Calling it again moves on, since records read
                         by readers are left out.

Near-duplicates are read once: rows of the same cluster collapse to one representative
with a count. When a set is larger than --limit, up to a third of the picks are the most
repeated groups (what is typical), up to a third the most salient (what is unusual), and
the rest are spread evenly over time; picks are returned in time order.

Each record's text holds every non-empty field within --chars. A record too long for that
is excerpted, not cut at the head: short fields are kept whole, and long values keep their
first lines for context plus the most informative of the rest (lines rare in that field,
with content signals such as hosts, paths, commands, code), in their original order, with
markers where lines were left out. Boilerplate lines and lines repeated within the record
are dropped first. Detail buried deep in a long record can then still reach a reader.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter

from . import coverage
from .fmt import fmt_time, ref, ref_id
from .index import Index
from .query import _parse_where
from .signals import signal_set

DEFAULT_LIMIT = 60
DEFAULT_CHARS = 1500
SHORT_FIELD = 200  # values up to this long are kept whole
HEAD_LINES = 2  # first lines of a long value, always kept for context
PIECE = 240  # long single lines are split into pieces of about this many characters
_LINE_FREQ: dict[tuple[str, str], Counter] = {}  # (table, field) -> line counts, built on demand


def _flat_value(v) -> str:
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def _norm_line(line: str) -> str:
    return re.sub(r"\d+", "0", line.strip().lower())[:200]


def _line_freq(idx: Index, table: str, field: str) -> Counter:
    """In how many clusters of a field each (digit-normalised) line occurs. Counting per
    cluster, not per value, keeps the many versions of one page or message from making
    its lines look common."""
    key = (table, field)
    if key not in _LINE_FREQ:
        by_unit: dict[str, set[str]] = {}
        for i, r in enumerate(idx.tables[table].rows):
            v = r.get(field)
            if isinstance(v, str) and "\n" in v:
                unit = idx.row_cluster.get((table, field, i), f"row{i}")
                by_unit.setdefault(unit, set()).update(_norm_line(x) for x in v.splitlines() if x.strip())
        cnt: Counter = Counter()
        for lines in by_unit.values():
            cnt.update(lines)
        _LINE_FREQ[key] = cnt
    return _LINE_FREQ[key]


def _entity_rarity(idx: Index, line: str) -> float:
    """0..1: how rare the rarest host, IP or path in the line is across the corpus (IDF over
    rows; 0 when the line names none)."""
    from .entities import IPV4, PATH, hosts

    n = sum(len(t.rows) for t in idx.tables.values()) or 1
    best = 0.0
    names = [("host", h) for h in hosts(line)] + [("ip", m.group(0)) for m in IPV4.finditer(line)]
    names += [("path", m.group(0)) for m in PATH.finditer(line)]
    for kind, value in names:
        stat = idx.entities.get((kind, value))
        rows = stat.rows if stat else 1
        best = max(best, math.log(n / max(rows, 1)) / math.log(n) if n > 1 else 0.0)
    return best


def _pieces(text: str) -> list[tuple[str, str]]:
    """(piece, normalised source line): long lines are split into pieces of about PIECE
    characters, each scored as its source line."""
    out = []
    for line in text.splitlines():
        src = _norm_line(line)
        while len(line) > PIECE * 1.5:
            cut = line.rfind(" ", PIECE // 2, PIECE)
            cut = cut if cut > 0 else PIECE
            out.append((line[:cut], src))
            line = line[cut:].lstrip()
        out.append((line, src))
    return out


def excerpt(idx: Index, table: str, field: str, text: str, budget: int) -> str:
    """The value within ``budget`` characters: its first lines, then the most informative
    other lines (rare in this field, with content signals), in original order."""
    if len(text) <= budget:
        return text
    pieces = _pieces(text)
    lines = [p for p, _ in pieces]
    bp = idx.boilerplate.get((table, field), set())
    freq = _line_freq(idx, table, field)
    seen: set[str] = set()
    cands = []  # (score, index)
    for k, (line, src) in enumerate(pieces):
        n = _norm_line(line)
        if not n or line.strip() in bp or n in seen:
            continue
        seen.add(n)
        if k < HEAD_LINES:
            cands.append((math.inf, k))
            continue
        rarity = 1.0 / (1.0 + math.log1p(max(freq.get(src, 1) - 1, 0)))
        richness = (1.0 + len(signal_set(line)) + min(len(n), 120) / 120) * (1.0 + 2.0 * _entity_rarity(idx, line))
        cands.append((rarity * richness, k))
    keep, used = set(), 0
    for _score, k in sorted(cands, key=lambda c: (-c[0], c[1])):
        cost = len(lines[k]) + 1
        if used + cost > budget:
            continue
        keep.add(k)
        used += cost
    out, gap = [], 0
    for k, line in enumerate(lines):
        if k in keep:
            if gap:
                out.append(f"[… {gap} lines left out]")
                gap = 0
            out.append(line)
        elif line.strip():
            gap += 1
    if gap:
        out.append(f"[… {gap} lines left out]")
    return "\n".join(out)


def record_text(idx: Index, table: str, row: int, chars: int) -> str:
    """Every non-empty field within ``chars``: short fields whole, long values excerpted
    with the remaining budget shared in proportion to their length."""
    items = [(k, _flat_value(v)) for k, v in idx.tables[table].rows[row].items() if v not in (None, "", [])]
    whole = sum(len(k) + len(v) + 2 for k, v in items)
    if whole <= chars:
        return "\n".join(f"{k}: {v}" for k, v in items)
    short = [(k, v) for k, v in items if len(v) <= SHORT_FIELD]
    long_ = [(k, v) for k, v in items if len(v) > SHORT_FIELD]
    left = max(chars - sum(len(k) + len(v) + 2 for k, v in short), 300 * max(1, len(long_)))
    total_long = sum(len(v) for _, v in long_) or 1
    parts = []
    for k, v in items:
        if len(v) <= SHORT_FIELD:
            parts.append(f"{k}: {v}")
        else:
            budget = max(200, int(left * len(v) / total_long))
            parts.append(f"{k}: " + (excerpt(idx, table, k, v, budget) if len(v) > budget else v))
    text = "\n".join(parts)
    # Safety net (e.g. a record of many short fields): never far beyond the budget.
    return text if len(text) <= chars + 300 else text[:chars] + " … [truncated]"


def _group_key(idx: Index, table: str, row: int) -> str:
    """Rows sharing a cluster in the file's main text field are near-duplicates."""
    for f in idx.profiles[table].text_fields[:1]:
        cid = idx.row_cluster.get((table, f, row))
        if cid:
            return cid
    return ref(table, row)


def _time_key(idx: Index, table: str, row: int):
    t = idx.time_of(table, row)
    return (t is None, t.replace(tzinfo=None) if t else 0, table, row)


def _resolve(idx: Index, spec: str, where: list[str] | None, n: int) -> tuple[list[tuple[str, int]], str, str]:
    """(rows, selection mode, description) or raises ValueError. Modes: ``collapse`` (group
    near-duplicates), ``theme`` (one row per cluster, weighted by cluster size), ``spread``
    (evenly over time)."""
    kind, _, arg = spec.partition(":")
    unit = idx.by_id.get(spec)
    if unit is not None:
        if unit.kind == "theme":
            return [(unit.table, r) for r in unit.members], "theme", f"theme {spec} (one record per cluster)"
        return [(unit.table, r) for r in unit.members], "spread", f"{unit.kind} {spec}"
    if kind == "grep" and arg:
        rx = re.compile(arg, 0 if any(ch.isupper() for ch in arg) else re.I)
        rows = [(name, i) for name, t in idx.tables.items() for i, r in enumerate(t.rows)
                if any(rx.search(_flat(v)) for v in r.values() if v not in (None, ""))]
        return rows, "collapse", f"rows matching /{arg}/"
    if kind == "pivot" and arg:
        low = arg.lower()
        rows = [(name, i) for name, t in idx.tables.items() for i, r in enumerate(t.rows)
                if any(low in _flat(v).lower() for v in r.values() if v not in (None, ""))]
        return rows, "collapse", f"rows containing {arg!r}"
    if kind == "rows" and arg:
        if arg not in idx.tables:
            raise ValueError(f"unknown table {arg!r}; tables: {', '.join(idx.tables)}")
        keep = _parse_where(where)
        rows = [(arg, i) for i, r in enumerate(idx.tables[arg].rows) if keep(r)]
        return rows, "collapse", f"{arg} rows" + (f" where {' and '.join(where)}" if where else "")
    if kind == "around" and arg:
        p = _lookup(idx, arg)
        if not p:
            raise ValueError(f"unknown ref {arg!r}")
        order = idx.order[p[0]]
        k = order.index(p[1])
        return [(p[0], r) for r in order[max(0, k - n) : k + n + 1]], "spread", f"{2 * n + 1} rows around {arg}"
    if kind == "refs" and arg:
        rows = []
        for s in arg.split(","):
            p = _lookup(idx, s.strip())
            if not p:
                raise ValueError(f"unknown ref {s.strip()!r}")
            rows.append(p)
        return rows, "spread", f"{len(rows)} given rows"
    raise ValueError(f"unknown set {spec!r}; use cNN, tNN, wNN, grep:REGEX, pivot:VALUE, rows:TABLE "
                     "(with --where), around:REF or refs:REF,REF")


def _flat(v) -> str:
    return " ".join(str(x) for x in v) if isinstance(v, list) else str(v)


def _lookup(idx: Index, s: str) -> tuple[str, int] | None:
    table, _, line = s.rpartition(":")
    if table in idx.tables and line.isdigit() and 1 <= int(line) <= len(idx.tables[table].rows):
        return table, int(line) - 1
    return idx.id_lookup.get(s)


def _salience(idx: Index, table: str, row: int) -> float:
    for f in idx.profiles[table].text_fields:
        cid = idx.row_cluster.get((table, f, row))
        if cid:
            return idx.by_id[cid].score
    return 0.0


def select(idx: Index, rows: list[tuple[str, int]], mode: str, limit: int) -> list[dict]:
    """Groups of near-duplicates (or single rows), at most ``limit``, in time order. Over the
    limit, ``spread`` samples evenly over time; the other modes mix typical, unusual and
    evenly spread picks (see the module docstring)."""
    groups: dict[str, list[tuple[str, int]]] = {}
    for tr in rows:
        key = _group_key(idx, *tr) if mode == "collapse" else ref(*tr)
        groups.setdefault(key, []).append(tr)
    items = [{"rows": sorted(g, key=lambda tr: _time_key(idx, *tr))} for g in groups.values()]
    items.sort(key=lambda it: _time_key(idx, *it["rows"][0]))
    if len(items) <= limit:
        return items
    if mode == "spread":
        step = len(items) / limit
        return [items[int(k * step)] for k in range(limit)]

    def weight(it) -> int:
        if mode == "theme":
            table, row = it["rows"][0]
            sizes = [idx.by_id[c].size for f in idx.profiles[table].text_fields
                     if (c := idx.row_cluster.get((table, f, row)))]
            return max(sizes, default=1)
        return len(it["rows"])

    # Up to a third each: the heaviest groups (what is typical) and the most salient (what
    # is unusual), when those signals exist; the rest evenly over time, so a set of equally
    # plain records is sampled across its span rather than from its start.
    third = max(1, limit // 3)
    picked: dict[int, dict] = {}
    for it in sorted((it for it in items if weight(it) > 1), key=lambda it: -weight(it))[:third]:
        picked[id(it)] = it
    salient = sorted((it for it in items if id(it) not in picked and _salience(idx, *it["rows"][0]) > 0),
                     key=lambda it: -_salience(idx, *it["rows"][0]))
    for it in salient[:third]:
        picked[id(it)] = it
    rest = [it for it in items if id(it) not in picked]
    need = limit - len(picked)
    if need > 0 and rest:
        step = len(rest) / need
        for k in range(min(need, len(rest))):
            it = rest[int(k * step)]
            picked[id(it)] = it
    return sorted(picked.values(), key=lambda it: _time_key(idx, *it["rows"][0]))


SWEEP_THEMES = 12  # themes sampled per sweep
SWEEP_PER_THEME = 2


def sweep_rows(idx: Index, limit: int) -> list[tuple[str, int, str]]:
    """(table, row, why) for a sweep of up to ``limit`` records the agent has not read,
    opened or been shown in a listing, skipping atlas's most salient rare records (its own
    listings show those): windows for low-redundancy files (up to half), a few
    theme examples (an eighth), mostly salient rare records (diversified; at least half),
    a twentieth the most repeated records, the rest spread over time. Grouped by source,
    so a reader's batch is coherent."""
    from .cli import is_trivial  # cli imports this module

    opened, seen_rows, listed = coverage.load()
    done = coverage.load_read() | seen_rows
    # Units the agent has seen (opened, or as a listing line), plus atlas's most salient rare
    # records, which `overview` and `unseen` exist to show: the crew complements atlas.
    shown = opened | listed | set(idx.top_salient)
    picked: list[tuple[str, int, str]] = []
    have: set[tuple[str, int]] = set()

    def add(table: str, row: int, why: str) -> bool:
        if (table, row) in have or ref(table, row) in done or len(picked) >= limit:
            return False
        have.add((table, row))
        picked.append((table, row, why))
        return True

    def first_new(c) -> int | None:
        return next((r for r in c.members if ref(c.table, r) not in done), None)

    # Windows first for low-redundancy files (one long narrative reads best in context).
    if idx.windows:
        quota = limit // 2
        for w in sorted((w for w in idx.windows if w.cid not in opened), key=lambda w: -w.score):
            if len(picked) + len(w.members) > quota:
                break
            for r in w.members:
                add(w.table, r, f"window {w.cid}")
    # Theme examples (a few, from clusters not shown yet): the most typical cluster, the
    # largest and the most salient one. Atlas's overview already shows what themes are about.
    theme_quota = len(picked) + limit // 8
    for th in sorted(idx.themes, key=lambda t: (-t.actors, -t.rows))[:SWEEP_THEMES]:
        members = [idx.by_id[c] for c in th.clusters]
        cands = [idx.by_id[th.example]] if th.example in idx.by_id else []
        cands += sorted(members, key=lambda c: -c.size)[:2] + sorted(members, key=lambda c: -c.score)[:2]
        cands = [c for c in cands if c.cid not in shown]
        n = 0
        for c in cands:
            r = first_new(c)
            if r is not None and n < SWEEP_PER_THEME and len(picked) < theme_quota and add(c.table, r, f"theme {th.tid}"):
                n += 1
    # The most salient rare records, diversified so they are not variants of one topic.
    from .fmt import diversify

    # Rare records the agent has not been shown in any listing: the sweep goes past what
    # atlas already put in front of it, into the long tail.
    rare_quota = len(picked) + max(limit // 2, (limit - len(picked)) * 4 // 5)
    small = [c for c in idx.clusters if c.size <= 5 and c.cid not in shown and not is_trivial(idx, c)]
    for c in diversify(idx, sorted(small, key=lambda c: -c.score)):
        if len(picked) >= rare_quota:
            break
        r = first_new(c)
        if r is not None:
            add(c.table, r, f"rare {c.cid}")
    # The most repeated records (what most of the corpus says).
    rep_quota = len(picked) + max(1, limit // 20)
    for c in sorted((c for c in idx.clusters if c.size > 5 and c.cid not in shown and not is_trivial(idx, c)),
                    key=lambda c: -c.size):
        if len(picked) >= rep_quota:
            break
        r = first_new(c)
        if r is not None:
            add(c.table, r, f"repeated {c.cid} ×{c.size}")
    # The rest spread over time across all files.
    rest = [(t, r) for t in idx.tables for r in idx.order[t] if (t, r) not in have and ref(t, r) not in done]
    need = limit - len(picked)
    if need > 0 and rest:
        step = len(rest) / need
        for k in range(min(need, len(rest))):
            add(*rest[int(k * step)], "spread over time")
    return picked


def cmd_records(idx: Index, args) -> str:
    if args.set == "sweep":
        return _sweep_json(idx, args)
    try:
        rows, mode, desc = _resolve(idx, args.set, args.where, args.n)
    except (ValueError, re.error) as e:
        return json.dumps({"error": str(e)})
    items = select(idx, rows, mode, args.limit)
    out = []
    for it in items:
        table, row = it["rows"][0]
        out.append(_record(idx, table, row, args.chars, it["rows"][1:]))
    groups = len({_group_key(idx, *tr) for tr in rows}) if mode == "collapse" else len(rows)
    return json.dumps({"set": args.set, "description": desc, "rows": len(rows), "distinct": groups,
                       "returned": len(out), "records": out}, ensure_ascii=False)


def _record(idx: Index, table: str, row: int, chars: int, dups: list[tuple[str, int]] = ()) -> dict:
    return {
        "ref": ref(table, row),
        "id": idx.native_id(table, row),
        "cite": ref_id(idx, table, row),
        "time": fmt_time(idx.time_of(table, row)),
        "actor": " ".join(f"{f}={v}" for f, v in idx.actor_of(table, row)),
        "duplicates": len(dups),
        "duplicate_refs": [ref(*tr) for tr in dups[:5]],
        "text": record_text(idx, table, row, chars),
    }


def _sweep_json(idx: Index, args) -> str:
    picked = sweep_rows(idx, args.limit)
    out = []
    for table, row, why in picked:
        rec = _record(idx, table, row, args.chars)
        rec["source"] = why
        out.append(rec)
    total = sum(len(t.rows) for t in idx.tables.values())
    return json.dumps({"set": "sweep", "description": "a sweep of the corpus's most informative unread records",
                       "rows": total, "distinct": total, "returned": len(out), "sweep": True,
                       "records": out}, ensure_ascii=False)


def cmd_mark(idx: Index, args) -> str:
    """Log rows as read by a reader (`atlas mark REF ...`); `unseen` counts them."""
    refs = [r for r in args.refs if _lookup(idx, r)]
    coverage.record("crew-read", [args.label or ""], read=refs)
    return f"marked {len(refs)} rows as read"
