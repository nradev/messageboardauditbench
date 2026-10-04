"""Reader notes kept by the reading crew: ranking, and which ones the agent has seen.

The crew appends every verified note to the notes file (``coverage.notes_path()``); a note
records whether the crew's own output showed it. Atlas shows more of them where the agent
already looks (a few in `unseen`) and all of them on request (`atlas notes`, which the
crew's `notes` action calls), and logs what it showed in the coverage log.

Ranking uses only signals atlas already relies on, nothing fitted to a corpus:
  * the salience of the note's record (rarity × richness, the score behind `unseen`);
  * the note's kind, as a mild modifier (the schema's unexpected, outcomes, actions and
    claims before open questions, actors and times);
  * novelty: records the agent opened or read in full come last;
  * diversity: one note per record, and notes sharing most of their words with a note
    already picked are passed over.
"""

from __future__ import annotations

import re

from . import coverage
from .index import Index

KIND_WEIGHT = {"unexpected": 1.0, "outcomes": 0.8, "actions": 0.8, "claims": 0.8, "answer": 0.8,
               "open_questions": 0.5, "actors": 0.4, "times": 0.4}
_WORDS = re.compile(r"[^\W_]{4,}")


def note_key(n: dict) -> str:
    return n.get("key") or f"{n.get('ref')}|{' '.join(str(n.get('quote', '')).lower().split())[:80]}"


def shown_keys() -> set[str]:
    """Notes already put in front of the agent: by the crew's output, or by atlas."""
    keys = {note_key(n) for n in coverage.load_notes() if n.get("shown")}
    for e in coverage.load_entries():
        if e.get("cmd") == "notes-shown":
            keys.update(e.get("args", ()))
    return keys


def _row(idx: Index, ref: str) -> tuple[str, int] | None:
    table, _, line = str(ref).rpartition(":")
    if table in idx.tables and line.isdigit() and 1 <= int(line) <= len(idx.tables[table].rows):
        return table, int(line) - 1
    return None


def _salience(idx: Index, table: str, row: int) -> float:
    for f in idx.profiles[table].text_fields:
        cid = idx.row_cluster.get((table, f, row))
        if cid:
            return idx.by_id[cid].score
    return 0.0


def ranked(idx: Index, notes: list[dict], skip_rows: set[tuple[str, int]] = frozenset(),
           select: bool = True) -> list[dict]:
    """Notes most worth a look first: salient records, informative kinds, records the agent
    has not read. Notes on ``skip_rows`` are left out. With ``select`` (a short push), only
    one note per record and none sharing most of its words with one already picked; without
    it (a full listing), every note, in the same order."""
    opened, seen_rows, _ = coverage.load()
    scored = []
    for n in notes:
        row = _row(idx, n.get("ref", ""))
        if row is None or row in skip_rows:
            continue
        read_by_agent = f"{row[0]}:{row[1] + 1}" in seen_rows
        score = _salience(idx, *row) * (0.75 + 0.25 * KIND_WEIGHT.get(n.get("kind"), 0.4))
        scored.append((read_by_agent, -score, n, row))
    scored.sort(key=lambda x: (x[0], x[1]))
    if not select:
        return [n for *_rest, n, _row in scored]
    out, rows, word_sets = [], set(), []
    for _read, _score, n, row in scored:
        if row in rows:
            continue
        words = set(_WORDS.findall(f"{n.get('note', '')} {n.get('quote', '')}".lower()))
        if words and any(len(words & w) >= 0.6 * min(len(words), len(w)) for w in word_sets):
            continue
        rows.add(row)
        word_sets.append(words)
        out.append(n)
    return out


def line(n: dict, words: int = 25) -> str:
    quote = " ".join(str(n.get("quote", "")).split()[:words])
    return f"({n.get('kind')}) {str(n.get('note', '')).strip()} — \"{quote}\" [{n.get('cite') or n.get('ref')}]"


def cmd_notes(idx: Index, args) -> str:
    """`atlas notes [REGEX]`: kept reader notes, those not shown before first, ranked."""
    notes = coverage.load_notes()
    if not notes:
        return "no reader notes yet: they are kept from the reading crew's brief, ask and sweep calls."
    try:
        rx = re.compile(args.pattern, re.I) if args.pattern else None
    except re.error as e:
        return f"bad regex: {e}"
    pool = [n for n in notes if rx is None or rx.search(f"{n.get('note', '')} {n.get('quote', '')} {n.get('cite', '')} "
                                                        f"{n.get('kind', '')}")]
    seen = shown_keys()
    order = (ranked(idx, [n for n in pool if note_key(n) not in seen], select=False)
             + ranked(idx, [n for n in pool if note_key(n) in seen], select=False))
    shown = order[: args.n]
    fresh = sum(1 for n in pool if note_key(n) not in seen)
    out = [f"reader notes{f' /{args.pattern}/' if args.pattern else ''}: {len(pool)} of {len(notes)} match, {fresh} not "
           f"shown before; listing {len(shown)} (not shown first, salient records first):"]
    out += ["- " + line(n) for n in shown]
    if len(order) > len(shown):
        out.append(f"… {len(order) - len(shown)} more: call again (shown notes move to the end) or narrow with a regex.")
    out.append("Quotes were checked against their records when stored; confirm what you rely on with atlas show.")
    coverage.record("notes-shown", [note_key(n) for n in shown])
    return "\n".join(out)


def unseen_section(idx: Index, k: int = 3) -> list[str]:
    """A few reader notes not shown yet, for the end of `atlas unseen` (nothing if none)."""
    notes = coverage.load_notes()
    if not notes:
        return []
    seen = shown_keys()
    # A record counts as covered once any note on it was shown; `atlas notes` lists the rest.
    seen_rows = {r for k in seen if (r := _row(idx, k.split("|", 1)[0]))}
    fresh = ranked(idx, [n for n in notes if note_key(n) not in seen], skip_rows=seen_rows)
    if not fresh:
        return []
    top = fresh[:k]
    coverage.record("notes-shown", [note_key(n) for n in top])
    out = [f"\nReader notes not shown yet ({len(fresh)} records; top {len(top)}, salient records first):"]
    out += ["  " + line(n, 20) for n in top]
    if len(fresh) > len(top):
        out.append(f"  {len(fresh) - len(top)} more: crew_notes (optionally with a regex).")
    return out
