"""Material for a final writer, and checks on what it writes (hidden commands).

`atlas writer pack DRAFT [--level W1|W2|W3]` prints, as JSON, what a fresh writer gets
next to the agent's draft, in order of value:

  W1  the gap check of the draft (Fix and Consider items)
  W2  + verified reader notes the draft does not use, ranked as everywhere else
  W3  + excerpts of the records the draft cites and of records the agent read at length
        but did not cite, and a short structural map of the corpus (context only)

`atlas writer check CANDIDATE --draft DRAFT --inputs FILE` prints, as JSON, the Fix
items of a rewritten report that the draft did not have, and the record refs it cites
that appear nowhere in the writer's inputs.

Neither command writes to the coverage log: the writer works after the investigation,
and what it reads is not the agent's coverage. Both ignore the agent's gap-check
dismissals.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from . import coverage
from .fmt import ref
from .gapcheck import _covered, normalize, parse_report, run
from .index import Index
from .notes import line as note_line
from .notes import ranked
from .records import _salience, record_text

LEVELS = ("W1", "W2", "W3")
MAX_NOTES = 30
MAX_CITED = 40
MAX_OPENED = 20
RECORD_CHARS = 800
MAP_CHARS = {"overview": 4000, "timeline": 2500, "anomalies": 2500}


@contextmanager
def _no_coverage():
    """Run atlas commands without logging them as the agent's coverage."""
    saved = coverage.record
    coverage.record = lambda *a, **k: None
    try:
        yield
    finally:
        coverage.record = saved


def _year(idx: Index) -> int:
    from datetime import date

    years = [p.fields[p.time_field].tmin.year for p in idx.profiles.values()
             if p.time_field and p.fields[p.time_field].tmin]
    return years[0] if years else date.today().year


def _cited(idx: Index, text: str) -> list[tuple[str, int]]:
    """Rows the text cites by ref or structured id, in order of first mention."""
    rep = parse_report(idx, text, _year(idx))
    hits = [(o, (t, r)) for t, r, o in rep.refs]
    hits += [(o, idx.id_lookup[i]) for i, o in rep.ids if i in idx.id_lookup]
    return list(dict.fromkeys(row for _o, row in sorted(hits)))


def _gapcheck_text(fixes, considers) -> str:
    out = [f"{len(fixes)} to fix, {len(considers)} to consider", "Fix (claims the data does not support as written):"]
    out += [f"  - {i.text}" for i in fixes] or ["  none"]
    out.append("Consider (optional; include only what is material):")
    out += [f"  - {i.text}" for i in considers] or ["  none"]
    return "\n".join(out)


def _excerpt(idx: Index, table: str, row: int) -> dict:
    return {"ref": ref(table, row), "text": record_text(idx, table, row, RECORD_CHARS)}


def _corpus_map(idx: Index) -> str:
    from types import SimpleNamespace

    from .anomalies import cmd_anomalies
    from .cli import cmd_overview
    from .timeline import cmd_timeline

    parts = []
    with _no_coverage():
        for name, fn in (("overview", cmd_overview), ("timeline", cmd_timeline), ("anomalies", cmd_anomalies)):
            try:
                text = fn(idx, SimpleNamespace(json=False, page=1))
            except Exception as e:  # a map section is optional
                text = f"({name} unavailable: {type(e).__name__})"
            cap = MAP_CHARS[name]
            parts.append(text if len(text) <= cap else text[:cap] + " … [cut]")
    return "\n\n".join(parts)


def pack(idx: Index, draft: str, level: str) -> dict:
    # Items the agent dismissed are shown again: dismissing quiets repeated checks during the
    # investigation, and the writer weighs Consider items afresh (they stay optional).
    fixes, considers = run(idx, draft, set())
    out = {"level": level, "fix": len(fixes), "consider": len(considers),
           "gapcheck": _gapcheck_text(fixes, considers), "notes": [], "cited": [], "opened": [], "map": ""}
    if level in ("W2", "W3"):
        cited = set(_cited(idx, draft))
        norm = normalize(draft)
        unused = [n for n in coverage.load_notes()
                  if not _covered(f"{n.get('quote', '')} {n.get('note', '')}", norm)]
        out["notes"] = [note_line(n) for n in ranked(idx, unused, skip_rows=cited)[:MAX_NOTES]]
    if level == "W3":
        cited_rows = _cited(idx, draft)
        out["cited"] = [_excerpt(idx, t, r) for t, r in cited_rows[:MAX_CITED]]
        _opened, seen, _listed = coverage.load()
        rows = []
        for s in seen:
            table, _, line = s.rpartition(":")
            if table in idx.tables and line.isdigit() and 1 <= int(line) <= len(idx.tables[table].rows):
                rows.append((table, int(line) - 1))
        rows = [r for r in rows if r not in set(cited_rows)]
        rows.sort(key=lambda tr: -_salience(idx, *tr))
        out["opened"] = [_excerpt(idx, t, r) for t, r in rows[:MAX_OPENED]]
        out["map"] = _corpus_map(idx)
    return out


def check(idx: Index, candidate: str, draft: str, inputs: str) -> dict:
    draft_fix = {i.gid for i in run(idx, draft, set())[0]}
    fixes = run(idx, candidate, set())[0]
    allowed = set(_cited(idx, inputs))
    outside = [ref(t, r) for t, r in _cited(idx, candidate) if (t, r) not in allowed]
    return {"fix": len(fixes), "new_fix": [i.text for i in fixes if i.gid not in draft_fix],
            "refs_outside": outside}


def cmd_writer(idx: Index, args) -> str:
    def read(p):
        return Path(p).read_text(errors="replace")

    try:
        if args.action == "pack":
            if args.level not in LEVELS:
                return json.dumps({"error": f"level must be one of {', '.join(LEVELS)}"})
            return json.dumps(pack(idx, read(args.path), args.level), ensure_ascii=False)
        if args.action == "check":
            if not args.draft or not args.inputs:
                return json.dumps({"error": "check needs --draft and --inputs"})
            return json.dumps(check(idx, read(args.path), read(args.draft), read(args.inputs)), ensure_ascii=False)
    except OSError as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"error": f"unknown action {args.action!r}; use pack or check"})
