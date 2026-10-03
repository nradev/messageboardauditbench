"""Value-centred commands: ``entities`` (what values exist, rare ones first), ``pivot`` (one
value across every file, in time order) and ``count`` (filtered group-by counts)."""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from . import coverage
from .entities import EXTRACTED
from .fmt import fmt_time, footer, paginate, ref_id, short_day, snip
from .index import Index

_LIST = 25


def _when(s) -> str:
    if not s.first:
        return "no time"
    return short_day(s.first) if short_day(s.first) == short_day(s.last) else f"{short_day(s.first)}→{short_day(s.last)}"


def _entity_line(value: str, s) -> str:
    who = ", ".join(f"{a}" + (f"×{n}" if n > 1 else "") for a, n in s.actors.most_common(2))
    more = f" +{len(s.actors) - 2}" if len(s.actors) > 2 else ""
    return f"  {snip(value, 70):<70} ×{s.rows:<5} {_when(s):<11} {who}{more}"


def cmd_entities(idx: Index, args) -> str:
    by_kind: dict[str, list] = defaultdict(list)
    for (kind, value), s in idx.entities.items():
        by_kind[kind].append((value, s))
    if args.kind:
        items = by_kind.get(args.kind)
        if not items:
            # A bare field name selects that field in every file (``--kind ip16``).
            kinds = [k for k in by_kind if k.endswith("." + args.kind)]
            items = [(f"{v}  [{k}]" if len(kinds) > 1 else v, s) for k in kinds for v, s in by_kind[k]]
        if not items:
            return f"unknown kind {args.kind!r}; kinds: {', '.join(sorted(by_kind))}"
        key = {"rare": lambda x: (x[1].rows, x[1].first is None, x[1].first or 0),
               "count": lambda x: -x[1].rows,
               "first": lambda x: (x[1].first is None, x[1].first or 0)}[args.sort]
        items = sorted(items, key=key)
        shown, note = paginate(items, args.page, _LIST)
        out = [f"{args.kind}: {len(items):,} distinct values, sorted by {args.sort} {note}",
               "  value                                                                  rows   seen        top actors"]
        out += [_entity_line(v, s) for v, s in shown]
        first = shown[0][0] if shown else ""
        out.append(footer(f"atlas pivot {first!r}" if first else "",
                          f"atlas entities --kind {args.kind} --page {args.page + 1}"
                          if len(items) > args.page * _LIST else ""))
        coverage.record("entities", [args.kind, args.sort, f"page={args.page}"])
        return "\n".join(out)
    # Summary: every kind with its size, then for each the commonest and the rarest values.
    order = [k for k in EXTRACTED if k in by_kind] + sorted(k for k in by_kind if k not in EXTRACTED)
    out = ["Entities: values found in fields (table.field) or extracted from text (host, ip, path, env, email).",
           "Rarest first within each kind: values seen in few rows are where one-off events hide."]
    for kind in order:
        items = by_kind[kind]
        rows = sum(s.rows for _, s in items)
        common = sorted(items, key=lambda x: -x[1].rows)[:3]
        rare = sorted((x for x in items if x not in common),
                      key=lambda x: (x[1].rows, x[1].first is None, x[1].first or 0))[: args.n]
        out.append(f"\n{kind}: {len(items):,} distinct, {rows:,} row mentions; commonest: " +
                   ", ".join(f"{snip(v, 30)}×{s.rows}" for v, s in common))
        out += [_entity_line(v, s) for v, s in rare]
    out.append("\n" + footer("atlas entities --kind KIND", "atlas pivot VALUE", "atlas count TABLE.FIELD"))
    coverage.record("entities", [])
    return "\n".join(out)


def _row_summary(idx: Index, table: str, row: int, value_rx: re.Pattern, value: str = "") -> str:
    """One line per row: short categorical fields, then a snippet around the match in text."""
    p = idx.profiles[table]
    r = idx.tables[table].rows[row]
    bits, shown = [], []
    fields = p.actor_fields + [f for f, fs in p.fields.items() if fs.role == "category"]
    for f in fields:
        v = r.get(f)
        if not isinstance(v, str) or not v or len(v) > 40 or f in (p.id_field, p.time_field):
            continue
        # Skip values that repeat one already shown (page, wiki/page, wiki~page ...).
        if v.lower() == value.lower() or any(v in s or s in v for s in shown):
            continue
        shown.append(v)
        bits.append(f"{f}={v}")
    ctx = ""
    for f in p.text_fields + [f for f in r if f not in p.text_fields]:
        v = r.get(f)
        if isinstance(v, str) and len(v) > 40:
            m = value_rx.search(v)
            if m:
                lo = max(0, m.start() - 90)
                ctx = f" | {f}: " + ("…" if lo else "") + snip(v[lo : m.end() + 120], 230)
                break
    return " ".join(bits[:6]) + ctx


def cmd_pivot(idx: Index, args) -> str:
    value = args.value
    if not value:
        return "give a value to pivot on, e.g. atlas pivot 10.0.0.5"
    value_rx = re.compile(re.escape(value), 0 if args.case else re.I)
    exact = args.exact
    hits = []  # (time, table, row, fields matched)
    fields_hit: Counter = Counter()
    for name, t in idx.tables.items():
        for i, r in enumerate(t.rows):
            matched = []
            for k, v in r.items():
                if isinstance(v, list):
                    vals = [str(x) for x in v]
                elif isinstance(v, (str, int, float)) and not isinstance(v, bool):
                    vals = [str(v)]
                else:
                    continue
                if any((x == value if exact else bool(value_rx.search(x))) for x in vals):
                    matched.append(k)
            if matched:
                hits.append((idx.time_of(name, i), name, i, matched))
                for k in matched:
                    fields_hit[f"{name}.{k}"] += 1
    if not hits:
        return f"no rows contain {value!r}" + ("" if exact else " (substring, case-insensitive)") + \
            ". Try a shorter value or atlas grep."
    hits.sort(key=lambda h: (h[0] is None, h[0].replace(tzinfo=None) if h[0] else 0, h[1], h[2]))
    times = [h[0] for h in hits if h[0]]
    out = [f"pivot {value!r}: {len(hits):,} rows" + (f", {fmt_time(min(times))} → {fmt_time(max(times))}" if times else "")
           + ("" if exact else "  (substring match; --exact for whole values)"),
           "  found in: " + ", ".join(f"{f}×{n}" for f, n in fields_hit.most_common(8))]
    # Who and what co-occurs with the value: actor and category fields of the matching rows.
    co: dict[str, Counter] = defaultdict(Counter)
    days: Counter = Counter()
    for when, name, i, _ in hits:
        p = idx.profiles[name]
        r = idx.tables[name].rows[i]
        if when:
            days[short_day(when)] += 1
        for f in p.actor_fields[:3] + [f for f, fs in p.fields.items() if fs.role == "category"]:
            v = r.get(f)
            if isinstance(v, str) and v and v.lower() != value.lower():
                co[f][v] += 1
    for f, cnt in sorted(co.items(), key=lambda kv: -sum(kv[1].values()))[:6]:
        out.append(f"  {f}: {len(cnt)} distinct; " + ", ".join(f"{snip(v, 30)}×{n}" for v, n in cnt.most_common(5)))
    if days:
        items = sorted(days.items())
        out.append("  per day: " + ", ".join(f"{d} {n}" for d, n in items[:20]) + (" …" if len(items) > 20 else ""))
    shown, note = paginate(hits, args.page, _LIST)
    out.append(f"\nTimeline {note}:")
    for when, name, i, _ in shown:
        out.append(f"  {fmt_time(when)}  {ref_id(idx, name, i)}  {_row_summary(idx, name, i, value_rx, value)}")
    nxt = f"atlas pivot {value!r} --page {args.page + 1}" if len(hits) > args.page * _LIST else ""
    first = shown[0]
    out.append(footer(f"atlas show {first[1]}:{first[2] + 1}", nxt, "atlas count TABLE.FIELD --where F=V"))
    coverage.record("pivot", [value, f"page={args.page}"])
    return "\n".join(out)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _compare(value, op: str, target: str) -> bool:
    """Ordered comparison: numeric when both sides are numbers, otherwise as text (which
    also orders ISO timestamps, e.g. time>=2026-06-18). Missing values never match."""
    if value in (None, "", []):
        return False
    a, b = _num(value), _num(target)
    left, right = (a, b) if a is not None and b is not None else (str(value), target)
    return {">": left > right, "<": left < right, ">=": left >= right, "<=": left <= right}[op]


def _parse_where(clauses: list[str]):
    """``F=V``, ``F!=V``, ``F~REGEX`` (search), ``F>V``, ``F<V``, ``F>=V``, ``F<=V``.
    Returns a row predicate; several clauses must all hold."""
    tests = []
    for c in clauses or []:
        m = re.match(r"^([^=!~<>]+)(>=|<=|!=|=|~|>|<)(.*)$", c)
        if not m:
            raise ValueError(f"bad --where {c!r}; use FIELD=VALUE, FIELD!=VALUE, FIELD~REGEX, "
                             "or FIELD>VALUE / < / >= / <= (numbers or text, e.g. times)")
        f, op, v = m.group(1).strip(), m.group(2), m.group(3).strip()
        if op == "~":
            rx = re.compile(v, re.I)
            tests.append(lambda r, f=f, rx=rx: r.get(f) is not None and bool(rx.search(str(r.get(f)))))
        elif op == "=":
            tests.append(lambda r, f=f, v=v: str(r.get(f)) == v)
        elif op == "!=":
            tests.append(lambda r, f=f, v=v: str(r.get(f)) != v)
        else:
            tests.append(lambda r, f=f, op=op, v=v: _compare(r.get(f), op, v))
    return lambda r: all(t(r) for t in tests)


def _bucket(idx: Index, table: str, row: int, by: str):
    if by in ("day", "hour", "month"):
        t = idx.time_of(table, row)
        if not t:
            return "(no time)"
        return t.strftime({"day": "%Y-%m-%d", "hour": "%Y-%m-%d %H:00", "month": "%Y-%m"}[by])
    v = idx.tables[table].rows[row].get(by)
    return "(none)" if v in (None, "") else str(v)


def cmd_count(idx: Index, args) -> str:
    table, _, fname = args.target.partition(".")
    if table not in idx.tables:
        return f"unknown table {table!r}; tables: {', '.join(idx.tables)}. Use TABLE.FIELD, or TABLE to count rows."
    rows = idx.tables[table].rows
    if fname and fname not in idx.profiles[table].fields:
        return f"unknown field {fname!r} in {table}; fields: {', '.join(idx.profiles[table].fields)}"
    try:
        keep = _parse_where(args.where)
    except (ValueError, re.error) as e:
        return str(e)
    selected = [i for i, r in enumerate(rows) if keep(r)]
    label = f"{table}" + (f" where {' and '.join(args.where)}" if args.where else "")
    out = [f"{label}: {len(selected):,} of {len(rows):,} rows"]
    if not selected:
        return out[0]
    if not fname and not args.by:
        args.by = "day" if idx.profiles[table].time_field else None
    values = Counter(str(rows[i].get(fname)) if rows[i].get(fname) not in (None, "") else "(none)"
                     for i in selected) if fname else Counter({"(rows)": len(selected)})
    top = [v for v, _ in values.most_common(args.top)]
    if fname:
        out.append(f"{fname}: {len(values):,} distinct values; top {min(args.top, len(values))}:")
        out += [f"  {snip(v, 60):<60} {n:>7,}" for v, n in values.most_common(args.top)]
        if len(values) > args.top:
            rest = sum(n for v, n in values.items() if v not in top)
            singles = sum(1 for n in values.values() if n == 1)
            out.append(f"  … {len(values) - args.top:,} more values, {rest:,} rows; {singles:,} values occur once")
    if args.by:
        if args.by not in ("day", "hour", "month") and args.by not in idx.profiles[table].fields:
            return f"unknown --by {args.by!r}; use day, hour, month or a field of {table}"
        out.append(f"\nby {args.by}" + (f" (rows per value of {fname}, top {len(top)})" if fname else "") + ":")
        grid: dict[str, Counter] = defaultdict(Counter)
        for i in selected:
            v = (str(rows[i].get(fname)) if rows[i].get(fname) not in (None, "") else "(none)") if fname else "(rows)"
            if v in top:
                grid[v][_bucket(idx, table, i, args.by)] += 1
        for v in top:
            buckets = grid[v]
            items = sorted(buckets.items()) if args.by in ("day", "hour", "month") else buckets.most_common(12)
            shown = items[: args.buckets]
            line = ", ".join(f"{snip(b, 30)} {n}" for b, n in shown)
            more = f" … +{len(items) - len(shown)} {args.by}s" if len(items) > len(shown) else ""
            out.append(f"  {snip(v, 40)}: {line}{more}")
    rarest = next((v for v in reversed(top) if v not in ("(none)", "(rows)")), None) if fname else None
    out.append(footer(f"atlas pivot {rarest!r} --exact" if rarest else "",
                      "atlas count TABLE.FIELD --where F=V --by day"))
    coverage.record("count", [args.target, *(args.where or []), f"by={args.by}"])
    return "\n".join(out)


def _field_values(v) -> list[str]:
    if v in (None, "", []):
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x not in (None, "")]
    return [str(v)]


def cmd_rows(idx: Index, args) -> str:
    """Rows of one file matching --where conditions, one line each: time, ref (with record
    id) and the chosen fields (default: actor and category fields plus a text snippet)."""
    if args.table not in idx.tables:
        return f"unknown table {args.table!r}; tables: {', '.join(idx.tables)}"
    p = idx.profiles[args.table]
    rows = idx.tables[args.table].rows
    try:
        keep = _parse_where(args.where)
    except (ValueError, re.error) as e:
        return str(e)
    if args.fields:
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]
        unknown = [f for f in fields if f not in p.fields]
        if unknown:
            return f"unknown field(s) {', '.join(unknown)} in {args.table}; fields: {', '.join(p.fields)}"
    else:
        cats = [f for f, fs in p.fields.items() if fs.role == "category"]
        fields = [f for f in p.actor_fields[:3] + cats if f not in (p.id_field, p.time_field)][:6] + p.text_fields[:1]
    selected = [i for i, r in enumerate(rows) if keep(r)]
    if args.sort == "time":
        selected.sort(key=lambda i: (idx.time_of(args.table, i) is None,
                                     idx.time_of(args.table, i).replace(tzinfo=None) if idx.time_of(args.table, i) else 0, i))
    elif args.sort in p.fields:
        selected.sort(key=lambda i: str(rows[i].get(args.sort, "")))
    else:
        return f"unknown --sort {args.sort!r}; use time or a field of {args.table}"
    if args.desc:
        selected.reverse()
    label = args.table + (f" where {' and '.join(args.where)}" if args.where else "")
    shown, note = paginate(selected, args.page, _LIST)
    out = [f"{label}: {len(selected):,} rows {note}"]
    for i in shown:
        r = rows[i]
        bits = []
        for f in fields:
            vals = _field_values(r.get(f))
            if vals:
                width = 160 if f in p.text_fields else 60
                bits.append(f"{f}={snip(', '.join(vals), width)}")
        out.append(f"  {fmt_time(idx.time_of(args.table, i))}  {ref_id(idx, args.table, i)}  " + "  ".join(bits))
    nxt = (f"atlas rows {args.table} " + " ".join(f"--where {w!r}" for w in args.where or [])
           + f" --page {args.page + 1}") if len(selected) > args.page * _LIST else ""
    out.append(footer(f"atlas show {args.table}:{shown[0] + 1}" if shown else "", nxt,
                      f"atlas count {args.table}.FIELD" + "".join(f" --where {w!r}" for w in args.where or [])))
    coverage.record("rows", [args.table, *(args.where or []), f"page={args.page}"])
    return "\n".join(out)


def cmd_join(idx: Index, args) -> str:
    """Overlap of the values of two fields (in the same or different files): how many
    distinct values and rows on each side have a match on the other, with examples."""
    sides = []
    for spec in (args.left, args.right):
        table, _, fname = spec.partition(".")
        if table not in idx.tables or fname not in idx.profiles[table].fields:
            tables = ", ".join(idx.tables)
            return f"unknown field {spec!r}; use TABLE.FIELD (tables: {tables}; see atlas profile)"
        counts: Counter = Counter()
        for r in idx.tables[table].rows:
            for v in _field_values(r.get(fname)):
                counts[v.lower() if args.i else v] += 1
        sides.append((spec, counts))
    (ln, lc), (rn, rc) = sides
    both = set(lc) & set(rc)
    only_l = set(lc) - both
    only_r = set(rc) - both

    def share(counts, keys):
        total = sum(counts.values())
        return f"{sum(counts[k] for k in keys):,}/{total:,} rows" if total else "0 rows"

    out = [f"join {ln} with {rn}" + (" (case-insensitive)" if args.i else "") + ":",
           f"  {ln}: {len(lc):,} distinct values; {len(both):,} also in {rn} ({share(lc, both)} matched)",
           f"  {rn}: {len(rc):,} distinct values; {len(both):,} also in {ln} ({share(rc, both)} matched)"]

    def examples(title, keys, counts):
        if not keys:
            return [f"\n{title}: none"]
        top = sorted(keys, key=lambda k: (-counts[k], k))[: args.n]
        rare = sorted(keys, key=lambda k: (counts[k], k))[: max(0, args.n - len(top))] if len(keys) > len(top) else []
        lines = [f"\n{title}: {len(keys):,} values; most frequent:"]
        lines += [f"  {snip(k, 70)}  ×{counts[k]}" for k in top]
        lines += [f"  {snip(k, 70)}  ×{counts[k]}" for k in rare]
        return lines

    out += examples("in both", both, lc)
    out += examples(f"only in {ln}", only_l, lc)
    out += examples(f"only in {rn}", only_r, rc)
    pick = sorted(only_l, key=lambda k: (lc[k], k))[:1] or sorted(both, key=lambda k: (lc[k], k))[:1]
    out.append(footer(f"atlas pivot {pick[0]!r} --exact" if pick else "", f"atlas join {rn} {ln}"))
    coverage.record("join", [ln, rn])
    return "\n".join(out)
