"""atlas: a map of a log corpus for investigators. Compress first, expand on request.

Commands print compact text; ``--json`` prints machine-readable output instead.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

from . import coverage
from .cluster import Cluster
from .fmt import (
    PAGE,
    diversify,
    fmt_time,
    footer,
    paginate,
    ref,
    ref_id,
    short_day,
    snip,
)
from .gapcheck import cmd_gapcheck
from .index import Index, build_index
from .query import cmd_count, cmd_entities, cmd_join, cmd_pivot, cmd_rows
from .themes import related
from .timeline import cmd_timeline

HELP = """atlas: a map of a log corpus. Compress first, expand on request.

  atlas overview                 start here: files, fields, themes (what is typical), rare records
  atlas themes [--field T.F]     topics shared by many records and actors (tNN); atlas expand tNN
  atlas profile [TABLE]          fields: roles, counts, top/rare values, time range and precision
  atlas clusters [--field T.F] [--sort salience|size|time] [--page N]
                                 list clusters of near-duplicate values (cNN) or windows (wNN)
  atlas expand ID [--n N]        open a theme (tNN), cluster (cNN) or window (wNN): span, actors, examples
  atlas show REF [--offset N]    one row in full: REF = file:line (1-based line in the source
                                 file, also valid in shell/python, e.g. logs:120) or the record's id
  atlas grep PATTERN [-i] [--field T.F] [--page N]
                                 regex search, hits grouped by cluster, rare hits first
  atlas unseen [--page N]        rare records not opened yet, new ones first (each call moves on)
  atlas entities [--kind K] [--sort rare|count|first]
                                 values to pivot on (field values, hosts, IPs, paths...), rare first
  atlas pivot VALUE [--exact]    every row in any file containing VALUE, as one timeline
  atlas count TABLE[.FIELD] [--where F=V|F!=V|F~RE|F>V|F<V|F>=V|F<=V ...] [--by day|hour|FIELD]
                                 filtered counts and group-bys, no scripting needed
  atlas rows TABLE [--where ...] [--fields a,b] [--sort time|FIELD] [--desc]
                                 matching rows, one line each, with their ids
  atlas timeline                 when activity starts, ends, peaks, changes level, goes quiet
  atlas gapcheck [REPORT]        check a report against the data: Fix (unsupported citations and
                                 quotes) and Consider (optional coverage questions); --dismiss gID
  atlas join A.FIELD B.FIELD [-i]
                                 which values of one field appear in another (overlap, examples)

Data directory: --data DIR or $ATLAS_DATA (default: current directory).
Field guesses can be overridden: --time-field F --actor-field F[,F] --text-field F[,F].
"""

def span(idx: Index, c: Cluster) -> tuple:
    times = [t for t in (idx.time_of(c.table, r) for r in c.members) if t]
    return (min(times), max(times)) if times else (None, None)


def top_actors(idx: Index, c: Cluster, k: int = 2) -> str:
    fields = idx.profiles[c.table].actor_fields[:2]
    parts = []
    for f in fields:
        cnt = Counter(str(idx.tables[c.table].rows[r].get(f)) for r in c.members
                      if idx.tables[c.table].rows[r].get(f) not in (None, ""))
        if cnt:
            vals = ", ".join(f"{v}" + (f"×{n}" if n > 1 else "") for v, n in cnt.most_common(k))
            more = f" +{len(cnt) - k}" if len(cnt) > k else ""
            parts.append(f"{f}={vals}{more}")
    return "; ".join(parts)


def related_note(idx: Index, c: Cluster) -> str:
    """How many other records share most of this one's topic words: an unusual variant of a
    widespread activity, or an isolated oddity."""
    ti = idx.topics.get((c.table, c.field))
    if c.kind != "cluster" or ti is None or c.cid not in ti.words:
        return ""
    n, words = related(ti, c.cid)
    themes = idx.cluster_themes.get(c.cid, [])
    in_themes = f"; in theme {', '.join(themes[:3])}" if themes else "; in no theme"
    rel = f"related: {n}" + (f" ({', '.join(words[:3])})" if words else "") if n else "related: none"
    return f"  {rel}{in_themes}"


def one_line(idx: Index, c: Cluster, width: int = 110, rel: bool = False) -> str:
    a, b = span(idx, c)
    when = short_day(a) if a == b or not b or short_day(a) == short_day(b) else f"{short_day(a)}→{short_day(b)}"
    sig = f" [{','.join(sorted(c.signals))}]" if c.signals else ""
    where = f"{c.table}.{c.field}" if c.kind == "cluster" else f"{c.table} rows"
    head = f"{c.cid:>6} ×{c.size:<5} {when:<11} {where}{sig}"
    who = top_actors(idx, c)
    text = c.template if c.template and c.size > 1 else idx.display_text(c, c.leader)
    note = related_note(idx, c) if rel else ""
    return f"{head}  {who}{note}\n         | {snip(text, width)}"


def is_trivial(idx: Index, c: Cluster) -> bool:
    """Placeholder-like records (``*``, ``<*>``, ``test``, a default page line): few letters
    once wildcards are removed. Listed by `clusters`, kept out of the overview."""
    text = c.template if c.template else idx.display_text(c, c.leader)
    fixed = [t for t in text.split() if t != "<*>"]
    return sum(ch.isalpha() for ch in " ".join(fixed)) < 12 or (c.template is not None and len(fixed) <= 2)


def theme_line(idx: Index, th, width: int = 120) -> str:
    unit = idx.by_id[th.tid]
    a, b = span(idx, unit)
    when = short_day(a) if not b or short_day(a) == short_day(b) else f"{short_day(a)}→{short_day(b)}"
    ex = idx.by_id[th.example]
    return (f"{th.tid:>6} {th.rows:>6,} rows, {th.actors:>4} actors, {len(th.clusters):,} clusters, {when}  "
            f"{th.table}.{th.field}: {', '.join(th.words)}\n         | e.g. {snip(idx.display_text(ex, ex.leader), width)}")


# ---------- commands ----------


def field_summary(idx: Index) -> list[str]:
    out = []
    for name, p in idx.profiles.items():
        bits = [f"time={p.time_field or '-'}"]
        if p.actor_fields:
            bits.append("actors=" + ",".join(p.actor_fields[:3]))
        if p.text_fields:
            bits.append("text=" + ",".join(p.text_fields[:3]))
        prec = p.fields[p.time_field].precision if p.time_field else {}
        if prec and prec.get("resolution") in ("day", "hour") or prec.get("on_midnight", 0) > 0.2:
            bits.append(f"!time precision: {prec['resolution']}")
        out.append(f"  {name:<14} {p.rows:>7,} rows  " + "  ".join(bits))
    return out


def corpus_span(idx: Index) -> str:
    lo, hi = None, None
    for p in idx.profiles.values():
        if p.time_field:
            f = p.fields[p.time_field]
            lo = f.tmin if lo is None or (f.tmin and f.tmin < lo) else lo
            hi = f.tmax if hi is None or (f.tmax and f.tmax > hi) else hi
    return f"{fmt_time(lo)} → {fmt_time(hi)}" if lo else "no time field found"


def cmd_overview(idx: Index, args) -> str:
    rows = sum(p.rows for p in idx.profiles.values())
    out = [f"atlas overview: {idx.data_dir}  {len(idx.tables)} files, {rows:,} rows, {corpus_span(idx)}",
           "Fields (guessed from values; override with --time-field/--actor-field/--text-field):"]
    out += field_summary(idx)
    by_field: dict[tuple, list[Cluster]] = {}
    for c in idx.clusters:
        by_field.setdefault((c.table, c.field), []).append(c)
    opened = []
    listed = []
    themes = sorted(idx.themes, key=lambda t: (-t.actors, -t.rows))[:8]
    if themes:
        out.append("\nThemes: what is typical, topics shared by many records and actors (atlas themes; atlas expand tNN):")
        out += [theme_line(idx, th, 100) for th in themes]
        listed += [th.tid for th in themes]
    total_vals = sum(c.size for c in idx.clusters) or 1
    small_fields = []
    for (table, f), cl in sorted(by_field.items(), key=lambda kv: -sum(c.size for c in kv[1])):
        n_vals = sum(c.size for c in cl)
        singles = sum(1 for c in cl if c.size == 1)
        if n_vals < 50:
            small_fields.append(f"{table}.{f} ({n_vals})")
            continue
        # Major fields carry long text; short fields (summaries, labels) get a few lines.
        mean_len = idx.profiles[table].fields[f].mean_len
        major = n_vals / total_vals >= 0.2 and mean_len >= 80
        n_big, n_sal = (3, 8) if major else (0, 2)
        out.append(f"\n{table}.{f}: {n_vals:,} values in {len(cl):,} clusters ({singles:,} singletons)")
        big = [c for c in sorted(cl, key=lambda c: -c.size) if c.size > 5 and not is_trivial(idx, c)][:n_big]
        if big:
            out.append("  Largest groups of repeated records:")
            out += ["  " + one_line(idx, c, 90) for c in big]
        sal = diversify(idx, sorted((c for c in cl if c.size <= 5 and not is_trivial(idx, c)),
                                    key=lambda c: -c.score))[:n_sal]
        if sal:
            out.append("  Rare records with the richest content (related = records sharing their topic words):")
            out += ["  " + one_line(idx, c, 110, rel=True) for c in sal]
        listed += [c.cid for c in big + sal]
    if small_fields:
        out.append("\nSmaller text fields (atlas clusters --field T.F): " + ", ".join(small_fields))
    if idx.windows:
        out.append(f"\nLow-redundancy tables (mostly unique rows) are also split into {len(idx.windows)} "
                   f"windows of consecutive rows: atlas clusters --windows")
    best = next((c.cid for c in sorted(idx.clusters, key=lambda c: -c.score) if c.size <= 5), None)
    out.append("\n" + footer(f"atlas expand {themes[0].tid}" if themes else "", f"atlas expand {best}" if best else "",
                             "atlas unseen", "atlas entities", "atlas grep PATTERN"))
    coverage.record("overview", [], opened=opened, listed=listed)
    return "\n".join(out)


def example_record(idx: Index, table: str) -> str:
    """The first row with the most fields present, values shortened, as one line."""
    rows = idx.tables[table].rows
    best = max(range(min(len(rows), 2000)), key=lambda i: sum(v not in (None, "", []) for v in rows[i].values()),
               default=None)
    if best is None:
        return "(empty)"
    parts = []
    for k, v in rows[best].items():
        if v in (None, "", []):
            continue
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        parts.append(f"{k}={snip(s, 60)}")
    return f"[{ref(table, best)}] " + "  ".join(parts)


def cmd_profile(idx: Index, args) -> str:
    out = []
    for name, p in idx.profiles.items():
        if args.table and name != args.table:
            continue
        out.append(f"== {name}: {p.rows:,} rows  (time={p.time_field or '-'}, id={p.id_field or '-'}, "
                   f"actors={','.join(p.actor_fields[:3]) or '-'}, text={','.join(p.text_fields) or '-'})")
        out.append("  example: " + example_record(idx, name))
        for fs in p.fields.values():
            line = f"  {fs.name:<22} {fs.role:<8} present {fs.present:>6,}  distinct {fs.distinct:>6,}"
            if fs.role == "time" and fs.tmin:
                pr = fs.precision
                line += (f"  {fmt_time(fs.tmin)} → {fmt_time(fs.tmax)}  resolution {pr['resolution']}, "
                         f"on-midnight {pr['on_midnight']:.0%}, dup {pr['duplicate']:.0%}")
            elif fs.role == "text":
                line += f"  mean len {fs.mean_len:.0f}, max {fs.max_len:,}"
            else:
                if fs.top and fs.top[0][1] == 1:
                    line += "  all values distinct, e.g. " + ", ".join(snip(str(v), 30) for v, _ in fs.top[:3])
                else:
                    top = ", ".join(f"{snip(str(v), 30)}×{n}" for v, n in fs.top[:4])
                    line += f"  top: {top}"
                if fs.rare:
                    line += "  rare: " + ", ".join(snip(str(v), 25) for v, _ in fs.rare[:3])
            out.append(line)
    out.append("\n" + footer("atlas overview", "atlas clusters --field TABLE.FIELD"))
    coverage.record("profile", [args.table or ""])
    return "\n".join(out)


def cmd_clusters(idx: Index, args) -> str:
    units = idx.windows if args.windows else idx.clusters
    if args.field:
        t, _, f = args.field.partition(".")
        units = [c for c in units if c.table == t and (not f or c.field == f)]
    if args.min_size:
        units = [c for c in units if c.size >= args.min_size]
    if args.max_size:
        units = [c for c in units if c.size <= args.max_size]
    if args.sort == "size":
        units = sorted(units, key=lambda c: -c.size)
    elif args.sort == "time":
        units = sorted(units, key=lambda c: (span(idx, c)[0] is None, span(idx, c)[0] or 0))
    elif not args.windows:
        units = diversify(idx, sorted(units, key=lambda c: -c.score))
    shown, note = paginate(units, args.page)
    out = [f"{len(units):,} {'windows' if args.windows else 'clusters'}, sorted by "
           f"{'order' if args.windows and args.sort == 'salience' else args.sort} {note}"]
    out += [one_line(idx, c) for c in shown]
    nxt = f"atlas clusters --page {args.page + 1}" + (f" --field {args.field}" if args.field else "") \
        if len(units) > args.page * PAGE else ""
    out.append(footer(f"atlas expand {shown[0].cid}" if shown else "", nxt))
    coverage.record("clusters", [args.field or "", args.sort, f"page={args.page}"] + (["windows"] if args.windows else []),
                    listed=[c.cid for c in shown])
    return "\n".join(out)


def _diverse(idx: Index, c: Cluster, n: int) -> list[int]:
    """Leader, then members spread over time and across actors."""
    if c.size <= n:
        return list(c.members)
    picks = [c.leader]
    seen_actor = {tuple(idx.actor_of(c.table, c.leader))}
    step = max(1, c.size // n)
    for r in c.members[step::step]:
        a = tuple(idx.actor_of(c.table, r))
        if a not in seen_actor or len(picks) < n // 2:
            picks.append(r)
            seen_actor.add(a)
        if len(picks) >= n - 1:
            break
    if c.members[-1] not in picks:
        picks.append(c.members[-1])
    return picks[:n]


def diff_lines(base: list[str], text: str, limit: int) -> str:
    lines = text.splitlines()
    base_set = {ln.strip() for ln in base}
    new_set = {ln.strip() for ln in lines}
    added = [ln for ln in lines if ln.strip() and ln.strip() not in base_set]
    removed = sum(1 for ln in base if ln.strip() and ln.strip() not in new_set)
    if not added and not removed:
        return "(same lines as the first row shown)"
    body = "\n".join("+ " + ln for ln in added)
    if len(body) > limit:
        body = body[:limit] + " …"
    return f"(differs from the first row shown: {len(added)} lines added, {removed} removed)\n{body}".rstrip()


def per_day(idx: Index, c: Cluster) -> str:
    days = Counter(short_day(t) for t in (idx.time_of(c.table, r) for r in c.members) if t)
    if not days:
        return ""
    items = sorted(days.items())
    if len(items) > 14:
        top = sorted(items, key=lambda kv: -kv[1])[:10]
        return f"active {len(items)} days, {items[0][0]} → {items[-1][0]}; busiest: " + \
            ", ".join(f"{d} {n}" for d, n in sorted(top))
    return "per day: " + ", ".join(f"{d} {n}" for d, n in items)


def cmd_expand(idx: Index, args) -> str:
    c = idx.by_id.get(args.id)
    if not c:
        return f"unknown id {args.id!r}; ids look like c12 (cluster), t3 (theme) or w3 (window). Try: atlas overview"
    if c.kind == "theme":
        return expand_theme(idx, c, args)
    a, b = span(idx, c)
    where = f"{c.table}.{c.field}" if c.kind == "cluster" else f"{c.table} rows (window)"
    out = [f"{c.cid}: {c.size:,} rows of {where}, {fmt_time(a)} → {fmt_time(b)}"]
    if c.template:
        out.append(f"template: {c.template}")
    if c.signals:
        out.append(f"signals: {', '.join(sorted(c.signals))}")
    for f in idx.profiles[c.table].actor_fields[:3]:
        cnt = Counter(str(idx.tables[c.table].rows[r].get(f)) for r in c.members
                      if idx.tables[c.table].rows[r].get(f) not in (None, ""))
        if cnt:
            out.append(f"{f}: {len(cnt)} distinct; " + ", ".join(f"{v}×{n}" for v, n in cnt.most_common(6)))
    pd = per_day(idx, c)
    if pd:
        out.append(pd)
    picks = list(c.members) if c.kind == "window" else _diverse(idx, c, args.n)
    per = 4000 if c.size == 1 else (300 if c.kind == "window" else 900)
    out.append(f"\n{len(picks)} of {c.size} rows" + (" (first = earliest; spread over time and actors):"
                                                    if c.kind == "cluster" and c.size > len(picks) else ":"))
    seen = []
    base_lines = None
    for r in picks:
        t = idx.time_of(c.table, r)
        who = " ".join(f"{f}={v}" for f, v in idx.actor_of(c.table, r))
        text = idx.text_of(c, r)
        out.append(f"--- {ref_id(idx, c.table, r)}  {fmt_time(t)}  {who}")
        if base_lines is not None and c.kind == "cluster":
            # Later members of a cluster: only what differs from the first one shown.
            out.append(diff_lines(base_lines, text, per))
        else:
            out.append(text if len(text) <= per else
                       text[:per] + f" … [+{len(text) - per:,} chars: atlas show {ref(c.table, r)}]")
            base_lines = text.splitlines()
        seen.append(ref(c.table, r))
    out.append(footer(f"atlas show {ref(c.table, picks[0])}", "atlas unseen", "atlas grep PATTERN"))
    coverage.record("expand", [args.id], opened=[c.cid], seen_rows=seen)
    return "\n".join(out)


def expand_theme(idx: Index, unit: Cluster, args) -> str:
    th = next(t for t in idx.themes if t.tid == unit.cid)
    a, b = span(idx, unit)
    out = [f"{th.tid}: theme in {th.table}.{th.field}: {th.rows:,} rows in {len(th.clusters):,} clusters, "
           f"{th.actors} actors, {fmt_time(a)} → {fmt_time(b)}",
           f"words: {', '.join(th.words)}"]
    biggest = sorted((idx.by_id[cid] for cid in th.clusters), key=lambda c: -c.size)[:5]
    out.append("largest clusters in it: " + ", ".join(f"{c.cid} ×{c.size}" for c in biggest))
    pd = per_day(idx, unit)
    if pd:
        out.append(pd)
    picks = _diverse(idx, unit, max(args.n, 8))
    out.append(f"\n{len(picks)} example records, spread over time and actors:")
    seen = []
    for r in picks:
        cid = idx.row_cluster.get((th.table, th.field, r))
        who = " ".join(f"{f}={v}" for f, v in idx.actor_of(th.table, r))
        out.append(f"--- {ref_id(idx, th.table, r)}  {fmt_time(idx.time_of(th.table, r))}  {who}  [{cid}]")
        text = idx.display_text(idx.by_id[cid], r) if cid else idx.text_of(unit, r)
        out.append(text if len(text) <= 500 else text[:500] + f" … [atlas show {ref(th.table, r)}]")
        seen.append(ref(th.table, r))
    out.append(footer(f"atlas expand {biggest[0].cid}" if biggest else "", f"atlas grep {th.words[0]!r}",
                      "atlas themes"))
    coverage.record("expand", [th.tid], opened=[th.tid], seen_rows=seen)
    return "\n".join(out)


def cmd_themes(idx: Index, args) -> str:
    themes = sorted(idx.themes, key=lambda t: (-t.actors, -t.rows))
    if args.field:
        t, _, f = args.field.partition(".")
        themes = [th for th in themes if th.table == t and (not f or th.field == f)]
    if not themes:
        return "no themes (fields with fewer than 100 clusters get none). Try atlas clusters."
    shown, note = paginate(themes, args.page)
    out = [f"{len(themes)} themes, most actors first {note}:"]
    out += [theme_line(idx, th) for th in shown]
    out.append(footer(f"atlas expand {shown[0].tid}", "atlas unseen"))
    coverage.record("themes", [args.field or "", f"page={args.page}"], listed=[th.tid for th in shown])
    return "\n".join(out)


def parse_ref(idx: Index, s: str) -> tuple[str, int] | None:
    table, _, line = s.rpartition(":")
    if table in idx.tables and line.isdigit() and 1 <= int(line) <= len(idx.tables[table].rows):
        return table, int(line) - 1
    unit = idx.by_id.get(s)  # a cluster or theme id: its first (earliest) record
    if unit is not None and unit.kind in ("cluster", "theme"):
        if unit.kind == "theme":
            th = next(t for t in idx.themes if t.tid == unit.cid)
            unit = idx.by_id[th.example]
        return unit.table, unit.leader
    return idx.id_lookup.get(s)  # the record's own id value


def cmd_show(idx: Index, args) -> str:
    p = parse_ref(idx, args.ref)
    if not p:
        return (f"unknown ref {args.ref!r}; use FILE:LINE (1-based line number, FILE one of "
                f"{', '.join(idx.tables)}), a record id, or a cluster/theme id (cNN, tNN)")
    table, row = p
    r = idx.tables[table].rows[row]
    out = [ref_id(idx, table, row)]
    if args.ref in idx.by_id:
        out[0] += f"  (first record of {args.ref}; atlas expand {args.ref} for the rest)"
    opened = []
    limit = 6000
    for k, v in r.items():
        if v in (None, "", []):
            continue
        cid = idx.row_cluster.get((table, k, row))
        tag = f"  [{cid}, ×{idx.by_id[cid].size}]" if cid else ""
        if cid:
            opened.append(cid)
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        if len(s) > 200:
            chunk = s[args.offset : args.offset + limit]
            rest = len(s) - args.offset - len(chunk)
            out.append(f"{k}:{tag} ({len(s):,} chars)\n{chunk}" +
                       (f"\n… [+{rest:,} chars: atlas show {args.ref} --offset {args.offset + limit}]" if rest > 0 else ""))
        else:
            out.append(f"{k}: {s}{tag}")
    out.append(footer(*(f"atlas expand {c}" for c in opened[:2]), "atlas unseen"))
    coverage.record("show", [args.ref], opened=opened, seen_rows=[ref(table, row)])
    return "\n".join(out)


def cmd_grep(idx: Index, args) -> str:
    try:
        # Smart case: case-insensitive unless the pattern has an upper-case letter (or -i).
        ignore = args.i or not any(ch.isupper() for ch in args.pattern)
        rx = re.compile(args.pattern, re.I if ignore else 0)
    except re.error as e:
        return f"bad regex: {e}. Escape special characters or use a simpler pattern."
    groups: dict[str, list[tuple[int, str]]] = {}  # cid -> [(row, matched text)]
    other: Counter = Counter()  # (table.field, value) for non-clustered fields
    other_rows: dict[tuple, list[str]] = {}
    hit_rows = set()
    want_t, _, want_f = (args.field or "").partition(".")
    for name, t in idx.tables.items():
        if want_t and name != want_t:
            continue
        for i, r in enumerate(t.rows):
            for k, v in r.items():
                if want_f and k != want_f:
                    continue
                if isinstance(v, list):
                    v = " ".join(str(x) for x in v)
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    v = str(v)  # numbers are searchable too
                if not isinstance(v, str) or not rx.search(v):
                    continue
                hit_rows.add((name, i))
                cid = idx.row_cluster.get((name, k, i))
                if cid:
                    groups.setdefault(cid, []).append((i, v))
                else:
                    key = (f"{name}.{k}", snip(v, 80))
                    other[key] += 1
                    other_rows.setdefault(key, []).append(ref(name, i))
    if not hit_rows:
        return f"no matches for /{args.pattern}/" + ("" if ignore else " (case-sensitive because of upper case; add -i)")
    clusters = [idx.by_id[c] for c in groups]
    big = sorted((c for c in clusters if c.size > 5), key=lambda c: -len(groups[c.cid]))
    small = diversify(idx, sorted((c for c in clusters if c.size <= 5), key=lambda c: -c.score))
    out = [f"/{args.pattern}/: {len(hit_rows):,} rows; {len(clusters):,} clusters "
           f"({len(small):,} small, {len(big):,} big); {len(other):,} distinct values in other fields"]
    seen = []
    if args.page == 1 and big:
        out.append("\nBig clusters with hits (one line each; atlas expand ID to open):")
        for c in big[:8]:
            out.append(f"{c.cid:>6} {len(groups[c.cid]):>4} hits of ×{c.size:<5} {c.table}.{c.field}  "
                       f"{top_actors(idx, c)}\n         | {snip(c.template if c.template and c.size > 1 else idx.display_text(c, c.leader), 110)}")
        if len(big) > 8:
            out.append(f"  … {len(big) - 8} more big clusters: atlas grep {args.pattern!r} --big")
    shown, note = paginate(small, args.page)
    if shown:
        out.append(f"\nSmall clusters with hits, most salient first {note}:")
        for c in shown:
            row, raw = groups[c.cid][0]
            text = idx.display_text(c, row)  # without boilerplate lines, when the match survives
            m = rx.search(text)
            if not m:
                text = raw
                m = rx.search(text)
            lo = max(0, m.start() - 220)
            ctx = ("…" if lo else "") + snip(text[lo : m.end() + 220], 460)
            t = idx.time_of(c.table, row)
            who = " ".join(f"{f}={v}" for f, v in idx.actor_of(c.table, row))
            sig = f" [{','.join(sorted(c.signals))}]" if c.signals else ""
            out.append(f"{c.cid:>6} ×{c.size} {ref_id(idx, c.table, row)} {fmt_time(t)} {who}{sig}"
                       f"{related_note(idx, c)}\n         | {ctx}")
            seen.append(ref(c.table, row))
    if args.page == 1 and other:
        out.append("\nOther fields (value ×rows):")
        for (fld, val), n in other.most_common(10):
            out.append(f"  {fld}: {val} ×{n}  e.g. {other_rows[(fld, val)][0]}")
        if len(other) > 10:
            out.append(f"  … {len(other) - 10} more values")
    if args.big:
        out = [out[0], "\nAll big clusters with hits:"] + [
            f"{c.cid:>6} {len(groups[c.cid]):>4} hits of ×{c.size:<5} | {snip(c.template if c.template and c.size > 1 else idx.display_text(c, c.leader), 110)}"
            for c in big]
    nxt = f"atlas grep {args.pattern!r} --page {args.page + 1}" if len(small) > args.page * PAGE else ""
    first = shown[0].cid if shown else (big[0].cid if big else "")
    out.append("\n" + footer(f"atlas expand {first}" if first else "", nxt, "atlas pivot VALUE", "atlas unseen"))
    coverage.record("grep", [args.pattern], seen_rows=seen, listed=[c.cid for c in shown])
    return "\n".join(out)


def cmd_unseen(idx: Index, args) -> str:
    """What the agent has not looked at yet. Three states per record: not shown anywhere,
    shown as a one-line entry in some listing, opened (expanded or shown in full). Records
    not shown yet come first, so calling `unseen` again brings up new material."""
    opened, seen_rows, listed = coverage.load()

    def is_opened(c: Cluster) -> bool:
        return c.cid in opened or any(ref(c.table, r) in seen_rows for r in c.members[:50])

    small = [c for c in idx.clusters if c.size <= 5 and not is_trivial(idx, c)]
    big = [c for c in idx.clusters if c.size > 5 and not is_trivial(idx, c)]
    ranked = diversify(idx, sorted((c for c in small if not is_opened(c)), key=lambda c: -c.score))
    fresh = [c for c in ranked if c.cid not in listed]
    shown_before = [c for c in ranked if c.cid in listed]
    rest = fresh + shown_before
    top = [idx.by_id[cid] for cid in idx.top_salient]
    out = [f"coverage of the {len(top)} most salient rare records: shown {sum(c.cid in listed for c in top)}, "
           f"opened {sum(is_opened(c) for c in top)}; all rare records: shown "
           f"{sum(c.cid in listed for c in small):,}, opened {sum(is_opened(c) for c in small):,} of {len(small):,}"]
    if idx.windows:
        w_open = sum(1 for w in idx.windows if is_opened(w))
        out[0] += f"; windows opened {w_open:,}/{len(idx.windows):,}"
    also_listed = []
    if args.page == 1:
        themes_left = [th for th in sorted(idx.themes, key=lambda t: (-t.actors, -t.rows))
                       if th.tid not in opened and th.tid not in listed]
        if themes_left:
            out.append(f"\nThemes not opened or shown yet ({len(themes_left)}; top 3):")
            out += [theme_line(idx, th, 90) for th in themes_left[:3]]
            also_listed += [th.tid for th in themes_left[:3]]
        big_left = sorted((c for c in big if not is_opened(c) and c.cid not in listed), key=lambda c: -c.size)
        if big_left:
            out.append(f"\nLargest repeated records not shown yet ({len(big_left)}; top 3):")
            out += [one_line(idx, c, 90) for c in big_left[:3]]
            also_listed += [c.cid for c in big_left[:3]]
    shown, note = paginate(rest, args.page)
    label = "not shown before" if shown and shown[0].cid not in listed else "shown before but not opened"
    out.append(f"\nRare records not opened, {label} first, most salient first {note}:")
    out += [one_line(idx, c, 120, rel=True) for c in shown]
    if fresh and len(fresh) < len(rest):
        out.append(f"  ({len(fresh):,} not shown before; {len(shown_before):,} shown in earlier lists but not opened)")
    if idx.windows:
        wins = [w for w in idx.windows if not is_opened(w)]
        out.append(f"\nUnopened windows ({len(wins)}), in order: " + " ".join(w.cid for w in wins[:30]) +
                   (" …" if len(wins) > 30 else ""))
    out.append("Records listed here count as shown: the next `atlas unseen` starts with new ones.")
    out.append(footer(f"atlas expand {shown[0].cid}" if shown else "", "atlas unseen"))
    coverage.record("unseen", [f"page={args.page}"], listed=[c.cid for c in shown] + also_listed)
    return "\n".join(out)


# ---------- entry point ----------


def to_json(idx: Index, args) -> str:
    """Minimal machine-readable output: the units a command would rank, with their stats."""
    units = idx.windows if getattr(args, "windows", False) else idx.clusters
    data = []
    for c in sorted(units, key=lambda c: -c.score):
        a, b = span(idx, c)
        data.append({"id": c.cid, "table": c.table, "field": c.field, "size": c.size, "score": round(c.score, 3),
                     "signals": sorted(c.signals), "first": fmt_time(a), "last": fmt_time(b),
                     "leader": ref(c.table, c.leader), "template": c.template})
    return json.dumps(data, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    commands = ("overview", "profile", "clusters", "expand", "show", "grep", "unseen", "entities", "pivot", "count",
                "themes", "rows", "join", "timeline", "gapcheck")
    # Accept options before the command too (`atlas --data DIR overview`).
    first = next((i for i, a in enumerate(argv) if a in commands), None)
    if first:
        argv = argv[first:] + argv[:first]
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", default=os.environ.get("ATLAS_DATA", "."))
    common.add_argument("--time-field")
    common.add_argument("--actor-field")
    common.add_argument("--text-field")
    common.add_argument("--json", action="store_true")
    ap = argparse.ArgumentParser(prog="atlas", add_help=False)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("overview", parents=[common])
    p = sub.add_parser("profile", parents=[common])
    p.add_argument("table", nargs="?")
    p = sub.add_parser("clusters", parents=[common])
    p.add_argument("--field")
    p.add_argument("--sort", choices=["salience", "size", "time"], default="salience")
    p.add_argument("--min-size", type=int)
    p.add_argument("--max-size", type=int)
    p.add_argument("--windows", action="store_true")
    p.add_argument("--page", type=int, default=1)
    p = sub.add_parser("expand", parents=[common])
    p.add_argument("id")
    p.add_argument("--n", type=int, default=6)
    p = sub.add_parser("show", parents=[common])
    p.add_argument("ref")
    p.add_argument("--offset", type=int, default=0)
    p = sub.add_parser("grep", parents=[common])
    p.add_argument("pattern")
    p.add_argument("-i", action="store_true")
    p.add_argument("--field")
    p.add_argument("--big", action="store_true")
    p.add_argument("--page", type=int, default=1)
    p = sub.add_parser("unseen", parents=[common])
    p.add_argument("--page", type=int, default=1)
    p = sub.add_parser("themes", parents=[common])
    p.add_argument("--field")
    p.add_argument("--page", type=int, default=1)
    p = sub.add_parser("entities", parents=[common])
    p.add_argument("--kind")
    p.add_argument("--sort", choices=["rare", "count", "first"], default="rare")
    p.add_argument("--n", type=int, default=6)
    p.add_argument("--page", type=int, default=1)
    p = sub.add_parser("pivot", parents=[common])
    p.add_argument("value")
    p.add_argument("--exact", action="store_true")
    p.add_argument("--case", action="store_true")
    p.add_argument("--page", type=int, default=1)
    sub.add_parser("timeline", parents=[common])
    p = sub.add_parser("gapcheck", parents=[common])
    p.add_argument("report", nargs="?")
    p.add_argument("--dismiss", nargs="+")
    p = sub.add_parser("rows", parents=[common])
    p.add_argument("table")
    p.add_argument("--where", action="append")
    p.add_argument("--fields")
    p.add_argument("--sort", default="time")
    p.add_argument("--desc", action="store_true")
    p.add_argument("--page", type=int, default=1)
    p = sub.add_parser("join", parents=[common])
    p.add_argument("left")
    p.add_argument("right")
    p.add_argument("-i", action="store_true")
    p.add_argument("--n", type=int, default=8)
    p = sub.add_parser("count", parents=[common])
    p.add_argument("target")
    p.add_argument("--where", action="append")
    p.add_argument("--by")
    p.add_argument("--top", type=int, default=12)
    p.add_argument("--buckets", type=int, default=20)
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        print("\n" + HELP)
        return 2
    if not args.cmd:
        print(HELP)
        return 2
    data = Path(args.data)
    if not data.is_dir():
        print(f"data directory not found: {data}. Pass --data DIR or set ATLAS_DATA.")
        return 2
    idx = build_index(data, {"time_field": args.time_field, "actor_field": args.actor_field,
                             "text_field": args.text_field})
    if not idx.tables:
        print(f"no supported files (.jsonl .json .csv .tsv .log .txt) under {data}")
        return 2
    if args.json and args.cmd in ("clusters", "unseen", "overview"):
        print(to_json(idx, args))
        return 0
    handler = {"overview": cmd_overview, "profile": cmd_profile, "clusters": cmd_clusters,
               "expand": cmd_expand, "show": cmd_show, "grep": cmd_grep, "unseen": cmd_unseen,
               "entities": cmd_entities, "pivot": cmd_pivot, "count": cmd_count, "themes": cmd_themes,
               "rows": cmd_rows, "join": cmd_join, "timeline": cmd_timeline, "gapcheck": cmd_gapcheck}[args.cmd]
    print(handler(idx, args))
    return 0
