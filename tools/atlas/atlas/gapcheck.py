"""Gap checker: mismatches between the corpus, what the investigator examined, and the report.

Two kinds of output (see tools/TOOL_IDEAS.md, "Plan: gap checker"):

* Fix: problems with what the report already says, which the data can settle exactly —
  a cited ref that points to nothing, an id-shaped token that is no record, a quote found
  verbatim only in a different record than the one cited, a long quote not found anywhere.
  Correcting these never adds topics. They must be (almost) never wrong, so anything
  ambiguous (near-matches, likely paraphrases) goes to Consider instead.
* Consider: things the report does not cover or covers unevenly — large or important parts
  of the corpus, time structure, records the agent examined, single-citation passages,
  dates outside the corpus. Phrased as questions; leaving out immaterial items is correct.

Deterministic, no model calls. Never edits the report.
"""

from __future__ import annotations

import bisect
import difflib
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from . import coverage
from .index import Index
from .profile import _ACTOR_NAME
from .timeline import describe, events_for, series_for

MIN_QUOTE_WORDS = 4
MIN_QUOTE_CHARS = 20
FIX_NOT_FOUND_WORDS = 6  # shorter unmatched quotes are only Consider items
NEAR_MATCH = 0.5  # below this similarity a long unmatched quote is "not found" (Fix); above it, Consider
MAX_FIX = 10
MAX_CONSIDER = 8

_MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
# A quote opens after a line start, space or bracket and closes before space or punctuation;
# its text cannot start with punctuation or space (that would pair the closing mark of one
# quote with the opening mark of the next).
_QUOTE = re.compile(
    r'(?:(?<=^)|(?<=[\s(\[—:]))"(?![\s),;:.])([^"\n]{' + str(MIN_QUOTE_CHARS - 1) + r',600}?[^\s"])"(?=$|[\s).,;:!?—\]])'
    r'|“([^”\n]{' + str(MIN_QUOTE_CHARS) + r',600})”', re.M)
_BLOCKQUOTE = re.compile(r"^\s*>\s?(.+)$", re.M)
_BACKTICK = re.compile(r"`([^`\n]{1,400})`")
_ISO_DATE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_MD_DATE = re.compile(r"(?<![\d-])(\d\d)-(\d\d)(?![\d-])")
_NAMED_DATE = re.compile(r"\b(\d{1,2})\s+([A-Z][a-z]{2,8})\b|\b([A-Z][a-z]{2,8})\.?\s+(\d{1,2})\b")
_ELISION = re.compile(r"\s*(?:…|\[\.\.\.\]|\[…\]|\.\.\.)\s*")
_TOKEN = re.compile(r"[\w~@:./%+-]{4,}")


@dataclass
class Item:
    kind: str  # "fix" or "consider"
    key: str  # stable identity, used for --dismiss
    text: str
    score: float = 0.0
    gid: str = ""

    def __post_init__(self):
        self.gid = "g" + hashlib.sha1(f"{self.kind}|{self.key}".encode()).hexdigest()[:6]


# ---------- text normalisation and the corpus haystack ----------


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = t.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    t = t.replace("–", "-").replace("—", "-")
    t = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>])", r"\1", t)  # Markdown escapes
    t = t.replace("`", "").replace("*", "")  # inline code and emphasis marks
    t = re.sub(r"\s+", " ", t)
    return t.strip().lower()


@dataclass
class Haystack:
    """All string values of every row, normalised and joined, with offsets to find rows."""

    text: str
    starts: list[int]
    rows: list[tuple[str, int]]
    pos: dict[tuple[str, int], int] = field(default_factory=dict)

    @classmethod
    def build(cls, idx: Index) -> Haystack:
        parts, starts, rows, pos = [], [], [], 0
        for table, t in idx.tables.items():
            for i, r in enumerate(t.rows):
                vals = []
                for v in r.values():
                    if isinstance(v, list):
                        vals += [str(x) for x in v]
                    elif isinstance(v, (str, int, float)) and not isinstance(v, bool):
                        vals.append(str(v))
                s = normalize(" ␞ ".join(vals))  # record-separator glyph between fields
                starts.append(pos)
                rows.append((table, i))
                parts.append(s)
                pos += len(s) + 1
        return cls("\x00".join(parts), starts, rows, {r: k for k, r in enumerate(rows)})

    def row_at(self, offset: int) -> tuple[str, int]:
        return self.rows[bisect.bisect_right(self.starts, offset) - 1]

    def row_text(self, k: int) -> str:
        end = self.starts[k + 1] - 1 if k + 1 < len(self.starts) else len(self.text)
        return self.text[self.starts[k] : end]

    def find_rows(self, needle: str, limit: int = 50) -> list[tuple[str, int]]:
        out, start = [], 0
        while len(out) < limit:
            j = self.text.find(needle, start)
            if j < 0:
                break
            row = self.row_at(j)
            if not out or out[-1] != row:
                out.append(row)
            start = j + 1
        return out


def _segments(quote: str) -> list[str]:
    return [s for s in (x.strip(" .,;:") for x in _ELISION.split(normalize(quote))) if s]


def match_quote(hay: Haystack, quote: str) -> list[tuple[str, int]]:
    """Rows containing every elision-separated segment of the quote, in order."""
    segs = _segments(quote)
    if not segs:
        return []
    cands = hay.find_rows(segs[0], limit=200)
    out = []
    for table, i in cands:
        text = hay.row_text(hay.pos[(table, i)]) if len(segs) > 1 else None
        if text is None:
            out.append((table, i))
            continue
        pos = 0
        for s in segs:
            pos = text.find(s, pos)
            if pos < 0:
                break
            pos += len(s)
        else:
            out.append((table, i))
    return out


def _best_window(q: str, text: str) -> tuple[float, str]:
    best, best_win = 0.0, ""
    step = max(5, len(q) // 5)
    for st in range(0, max(1, len(text) - len(q) + 1), step):
        win = text[st : st + len(q)]
        r = difflib.SequenceMatcher(None, q, win).ratio()
        if r > best:
            best, best_win = r, win
    return best, best_win


def near_match(hay: Haystack, quote: str, prefer: list | None = None) -> tuple[float, tuple[str, int] | None, str]:
    """Best similarity of the quote to some row: candidate rows by shared word 4-grams, then
    each elision-separated segment is compared with its best window; the score is the
    lowest segment score (every part of a quote has to be there)."""
    segs = [x for x in _segments(quote) if len(x.split()) >= 2] or _segments(quote)
    words = " ".join(segs).split()
    if len(words) < 4:
        return 0.0, None, ""
    votes: dict[tuple[str, int], int] = {}
    for seg in segs:
        ws = seg.split()
        grams = [" ".join(ws[k : k + 4]) for k in range(max(1, len(ws) - 3))]
        for g in grams:
            for row in hay.find_rows(g, limit=20):
                votes[row] = votes.get(row, 0) + 1
    cands = sorted(votes, key=votes.get, reverse=True)[:3] + list(prefer or [])
    best = (0.0, None, "")
    for row in dict.fromkeys(cands):
        text = hay.row_text(hay.pos[row])
        scores = [_best_window(seg, text) for seg in segs]
        worst = min(x[0] for x in scores)
        if worst > best[0]:
            best = (worst, row, max(scores, key=lambda x: x[0])[1])
    return best


# ---------- report parsing ----------


@dataclass
class Report:
    text: str
    paragraphs: list[str]
    refs: list[tuple[str, int, int]] = field(default_factory=list)  # (table, row0, offset)
    bad_refs: list[tuple[str, int]] = field(default_factory=list)  # (ref text, offset)
    ids: list[tuple[str, int]] = field(default_factory=list)  # (record id, offset)
    units: list[tuple[str, int]] = field(default_factory=list)  # (cluster/theme id, offset)
    quotes: list[tuple[str, int, int]] = field(default_factory=list)  # (quote, start, end)
    dates: list[tuple[date, int]] = field(default_factory=list)
    norm: str = ""


def _shape(token: str) -> str:
    return re.sub(r"\d+", "0", re.sub(r"[^\W\d_]+", "a", token))


def parse_report(idx: Index, text: str, year: int) -> Report:
    rep = Report(text, [p for p in re.split(r"\n\s*\n", text) if p.strip()], norm=normalize(text))
    names = sorted(idx.tables, key=len, reverse=True)
    ref_rx = re.compile(r"(?<![\w/])(" + "|".join(re.escape(n) for n in names) + r"):(\d+)\b")
    for m in ref_rx.finditer(text):
        table, line = m.group(1), int(m.group(2))
        if 1 <= line <= len(idx.tables[table].rows):
            rep.refs.append((table, line - 1, m.start()))
        else:
            rep.bad_refs.append((m.group(0), m.start()))
    for m in _TOKEN.finditer(text):
        tok = m.group(0).strip(".,;:)")
        # Only structured ids (with separators) count as citations; a plain word that happens
        # to be a record id (a name in a file keyed by names) is too ambiguous to judge by.
        if tok in idx.id_lookup and re.search(r"[~@:/]", tok):
            rep.ids.append((tok, m.start()))
    for m in re.finditer(r"\b([ct]\d+)\b", text):
        unit = idx.by_id.get(m.group(1))
        if unit is not None and unit.kind in ("cluster", "theme"):
            rep.units.append((m.group(1), m.start()))
    for m in _QUOTE.finditer(text):
        q = m.group(1) or m.group(2)
        parts = [x.strip() for x in q.split(",")]
        if len(parts) >= 3 and all(len(x.split()) == 1 for x in parts):
            continue  # a list of terms ("a, b, c, d"), not a quotation
        if len(q.split()) >= MIN_QUOTE_WORDS:
            rep.quotes.append((q, m.start(), m.end()))
    for m in _BLOCKQUOTE.finditer(text):
        line = m.group(1).strip()
        if _QUOTE.search(line):
            continue  # its quoted part is taken by the quote matcher
        q = re.split(r"\s+(?:—|--)\s+", line)[0].strip().strip('"“”')  # drop an attribution
        if len(q) >= MIN_QUOTE_CHARS and len(q.split()) >= MIN_QUOTE_WORDS:
            rep.quotes.append((q, m.start(), m.start() + len(m.group(0))))
    for m in _ISO_DATE.finditer(text):
        try:
            rep.dates.append((date(int(m[1]), int(m[2]), int(m[3])), m.start()))
        except ValueError:
            pass
    for m in _MD_DATE.finditer(text):
        try:
            rep.dates.append((date(year, int(m[1]), int(m[2])), m.start()))
        except ValueError:
            pass
    for m in _NAMED_DATE.finditer(text):
        day, mon = (m[1], m[2]) if m[1] else (m[4], m[3])
        mi = _MONTHS.get(mon.lower())
        if mi:
            try:
                rep.dates.append((date(year, mi, int(day)), m.start()))
            except ValueError:
                pass
    return rep


_SENTENCE_END = re.compile(r"[.!?][)\"”*_]*\s+[A-Z(*\[`\"“]|;\s|\n")


def _unit_rows(idx: Index, cid: str) -> set[tuple[str, int]]:
    unit = idx.by_id[cid]
    if unit.kind == "theme":
        th = next(t for t in idx.themes if t.tid == cid)
        return {(idx.by_id[c].table, r) for c in th.clusters for r in idx.by_id[c].members[:200]}
    return {(unit.table, r) for r in unit.members[:500]}


def _adjacent_cites(rep: Report, start: int, end: int, idx: Index) -> list[set[tuple[str, int]]]:
    """Citations attached to a quote: within ~80 characters before the opening mark or after
    the closing mark, with no sentence boundary in between. Each is a set of rows (a record
    for a ref or id, all members for a cluster or theme id)."""
    out = []

    other_quotes = [(a, b) for _q, a, b in rep.quotes if (a, b) != (start, end)]

    def between_quote(lo: int, hi: int) -> bool:
        return any(lo <= a and b <= hi for a, b in other_quotes)

    def near(o: int) -> bool:
        # After the quote: same sentence, no other quote in between ("quote" (cite)). A quote
        # that ends with . ! or ? ends its sentence unless the citation is attached to it
        # with a bracket, dash, comma or semicolon ("…done." — revisions:12).
        if end <= o <= end + 80:
            gap = rep.text[end:o]
            inner = rep.text[max(start, end - 3) : end].rstrip('"”*_ ')
            if inner.endswith((".", "!", "?")) and not re.match(r"\s*[(\[—–,;-]", gap):
                return False
            return not _SENTENCE_END.search(gap) and not between_quote(end, o)
        # Before the quote only when it introduces it (`id` says: "quote", `id` — "quote").
        if start - 80 <= o < start:
            gap = rep.text[o:start]
            close = gap.find("`", 1)
            tail = gap[close + 1 :] if close >= 0 else gap
            # It must introduce the quote directly: apart from bracketed metadata, no "and" or
            # comma between them ("`id` and the other post: "…"" belongs to the other post).
            opened_here = "(" in rep.text[max(0, o - 3) : o]  # "(`id`, …): " closes its own bracket
            plain = tail.split(")", 1)[1] if opened_here and ")" in tail else tail
            plain = re.sub(r"\([^()]*\)", "", plain)
            if re.search(r",|\band\b", plain):
                return False
            unbalanced = tail.count(")") > tail.count("(") + (1 if opened_here else 0)  # id in an earlier bracket
            return (not _SENTENCE_END.search(gap) and not between_quote(o, start) and not unbalanced
                    and re.search(r"[:—]\s*[*_]*\s*$", tail) is not None)
        return False

    out += [{(t, r)} for t, r, o in rep.refs if near(o)]
    out += [{idx.id_lookup[i]} for i, o in rep.ids if near(o)]
    out += [_unit_rows(idx, c) for c, o in rep.units if near(o)]
    return out


def _keys(idx: Index, table: str, row: int) -> set[str]:
    """Identifier-like values of a row (ids, actors, other short strings; not categories,
    times or text), used to tell whether two records concern the same entity."""
    p = idx.profiles[table]
    r = idx.tables[table].rows[row]
    out = set()
    for f, fs in p.fields.items():
        v = r.get(f)
        # Actor-named fields (users, IPs, labels...) span many documents, so sharing one says
        # nothing about two records being the same entity; document keys and names do.
        if fs.role in ("id", "actor", "other") and not _ACTOR_NAME.search(f) and isinstance(v, str) \
                and 3 <= len(v) <= 200:
            out.add(v)
    return out


def _entity_count(idx: Index, rows: list[tuple[str, int]]) -> int:
    """How many distinct entities the rows belong to: per file, the fewest distinct values
    over its actor fields (the field that groups them most, e.g. the page), summed over
    files. Many revisions of one page count once."""
    total = 0
    for table in {t for t, _ in rows}:
        mine = [r for t, r in rows if t == table]
        counts = []
        for f in idx.profiles[table].actor_fields:
            vals = {idx.tables[table].rows[r].get(f) for r in mine} - {None, ""}
            if vals:
                counts.append(len(vals))
        total += min(counts) if counts else len(mine)
    return total


def single_cites(cites: list[set]) -> list[set]:
    return [c for c in cites if len(c) == 1]


def _paragraph_cites(rep: Report, start: int, idx: Index) -> set[tuple[str, int]]:
    """Every citation (file:line ref or structured record id) in the quote's paragraph."""
    lo = rep.text.rfind("\n\n", 0, start) + 1
    hi = rep.text.find("\n\n", start)
    hi = len(rep.text) if hi < 0 else hi
    out = {(t, r) for t, r, o in rep.refs if lo <= o <= hi}
    out |= {idx.id_lookup[i] for i, o in rep.ids if lo <= o <= hi}
    for c, o in rep.units:
        if lo <= o <= hi:
            out |= _unit_rows(idx, c)
    return out


def _ref(table: str, row: int) -> str:
    return f"{table}:{row + 1}"


# ---------- checks ----------


def fix_checks(idx: Index, rep: Report, hay: Haystack) -> tuple[list[Item], list[Item]]:
    fixes, considers = [], []
    for txt, _o in rep.bad_refs:
        table, line = txt.rsplit(":", 1)
        fixes.append(Item("fix", f"badref|{txt}",
                          f"cited {txt} does not exist: {table} has {len(idx.tables[table].rows):,} lines"))
    # Id-shaped tokens that are no record and no value anywhere.
    id_shapes = {_shape(k) for k in list(idx.id_lookup)[:5000]}
    seen_tok = set()
    for m in _BACKTICK.finditer(rep.text):
        tok = m.group(1).strip()
        if tok in seen_tok or " " in tok or tok in idx.id_lookup or _shape(tok) not in id_shapes:
            continue
        seen_tok.add(tok)
        if not hay.find_rows(normalize(tok), limit=1):
            fixes.append(Item("fix", f"badid|{tok}", f"`{tok}` looks like a record id but no record or value has it"))
    for q, qs, qe in rep.quotes:
        rows = match_quote(hay, q)
        cites = _adjacent_cites(rep, qs, qe, idx)
        short = q if len(q) <= 90 else q[:87] + "…"
        if rows:
            # Citations come before or after their quote depending on the writer, so a quote
            # counts as wrongly cited only if no citation in its paragraph contains it.
            single = [c for c in cites if len(c) == 1]
            if single and not (_paragraph_cites(rep, qs, idx) & set(rows)):
                cited_rows = [next(iter(c)) for c in single[:2]]
                cited = ", ".join(_ref(*r) for r in cited_rows)
                where = ", ".join(_ref(*r) for r in rows[:3])
                # Same entity (shared id/actor value, e.g. another revision of the same page, or
                # a page's metadata row): a related record, not a wrong one. Ambiguous -> Consider.
                ckeys = set().union(*(_keys(idx, *r) for r in cited_rows))
                related = [r for r in rows if ckeys & _keys(idx, *r)]
                if _entity_count(idx, rows) >= 5:
                    considers.append(Item("consider", f"common|{q[:80]}",
                                          f'quote "{short}" is common text (in many records) and not in the cited '
                                          f"{cited}; cite a record that holds it?", score=0.2))
                elif related:
                    considers.append(Item("consider", f"related|{q[:80]}",
                                          f'quote "{short}" is not in the cited {cited} but in related record(s) '
                                          f"{', '.join(_ref(*r) for r in related[:3])} (same entity). Cite the record "
                                          "that holds the text?", score=0.45))
                else:
                    fixes.append(Item("fix", f"wrongcite|{q[:80]}",
                                      f'quote "{short}" is not in the cited {cited}; found verbatim in {where}'))
            continue
        segs = _segments(q)
        if len(segs) > 1 and all(hay.find_rows(sg, limit=1) for sg in segs):
            considers.append(Item("consider", f"stitched|{q[:80]}",
                                  f'quote "{short}" joins text from different records; cite each part separately?',
                                  score=0.5))
            continue
        cited_rows = sorted(set().union(*cites)) if cites else []
        score, row, window = near_match(hay, q, prefer=cited_rows[:50])
        if score >= NEAR_MATCH and row is not None:
            considers.append(Item("consider", f"near|{q[:80]}",
                                  f'quote "{short}" is not verbatim; closest text ({score:.0%} similar) in {_ref(*row)}: '
                                  f'"{window[:90]}". Quote it exactly, or present it as a paraphrase?', score=0.6))
        elif single_cites(cites) and len(normalize(q).split()) >= FIX_NOT_FOUND_WORDS:
            # Only a citation of a specific record claims the words come from it; a cluster or
            # theme mention is evidence for a claim, not a source for a quotation.
            hint = f"; closest text ({score:.0%} similar) in {_ref(*row)}" if row is not None and score >= 0.3 else ""
            fixes.append(Item("fix", f"notfound|{q[:80]}", f'quote "{short}" was not found verbatim in the data{hint}'))
        else:
            considers.append(Item("consider", f"shortq|{q[:80]}",
                                  f'quoted phrase "{short}" was not found in the data: a quote (then cite and match '
                                  f"it), or your own words?", score=0.3))
    return fixes[:MAX_FIX], considers


def _mentioned_dates(rep: Report) -> set[date]:
    return {d for d, _ in rep.dates}


def _date_hit(days: set[date], d: date, slack: int = 1) -> bool:
    return any(abs((x - d).days) <= slack for x in days)


def consider_checks(idx: Index, rep: Report, text_lower: str) -> list[Item]:
    items: list[Item] = []
    total_rows = sum(len(t.rows) for t in idx.tables.values()) or 1
    # Themes: topics shared by many actors that the report never names.
    max_actors = max((th.actors for th in idx.themes), default=1) or 1
    for th in sorted(idx.themes, key=lambda t: (-t.actors, -t.rows))[:10]:
        if th.actors < 10:
            continue  # with few actors (e.g. roles in one transcript) a theme is a topic, not a shared activity
        hits = sum(1 for w in th.words if re.search(r"\b" + re.escape(w) + r"\b", text_lower))
        if hits < 2:
            share = th.rows / max(len(idx.tables[th.table].rows), 1)
            items.append(Item("consider", f"theme|{th.tid}",
                              f"theme {th.tid} ({th.rows:,} rows, {th.actors} actors; {', '.join(th.words[:5])}) is not "
                              f"discussed. Is it material to your account? (atlas expand {th.tid})",
                              score=0.5 * share + 0.5 * th.actors / max_actors))
    # Time structure: peaks, sharp changes and ends of the main series the report never dates.
    days = _mentioned_dates(rep)
    seen_events: set[tuple] = set()
    for s in series_for(idx)[:12]:
        share = s.rows / total_rows
        for e in events_for(s):
            if e.kind not in ("peak", "rise", "fall", "end") or _date_hit(days, e.day):
                continue
            if e.kind == "end" and ("." in s.name or share < 0.1):
                continue  # last activity only for whole files
            sig = (e.kind, e.day, e.count)
            if sig in seen_events:
                continue  # the same event seen through another field (identical series)
            seen_events.add(sig)
            items.append(Item("consider", f"time|{s.name}|{e.kind}|{e.day}",
                              f"{describe(e)}; the report gives no date near it. Is it material?",
                              score=min(1.0, share + (0.3 if e.kind in ("rise", "fall") else 0.1))))
    # Files never referenced.
    for table, t in idx.tables.items():
        if not re.search(r"\b" + re.escape(table.lower()) + r"\b", text_lower) and not any(
                r[0] == table for r in rep.refs):
            items.append(Item("consider", f"file|{table}",
                              f"file {table} ({len(t.rows):,} rows) is never referenced. Does it matter to your account?",
                              score=0.5 * len(t.rows) / total_rows))
    # Dates outside the corpus range.
    lo = min((p.fields[p.time_field].tmin for p in idx.profiles.values() if p.time_field and p.fields[p.time_field].tmin),
             default=None)
    hi = max((p.fields[p.time_field].tmax for p in idx.profiles.values() if p.time_field and p.fields[p.time_field].tmax),
             default=None)
    if lo and hi:
        outside = sorted({d for d in days if d < lo.date() or d > hi.date()})
        if outside:
            items.append(Item("consider", "dates|" + ",".join(map(str, outside[:5])),
                              f"dates {', '.join(map(str, outside[:5]))} fall outside the data ({lo.date()} → "
                              f"{hi.date()}). Intended (e.g. a named date), or a slip?", score=0.4))
    # Examined but not written up; long passages resting on one citation.
    opened, _rows, _listed = coverage.load()
    for cid in sorted(opened):
        unit = idx.by_id.get(cid)
        if unit is None or unit.kind != "cluster":
            continue
        refs_in = {_ref(unit.table, r) for r in unit.members}
        if any(_ref(t, r) in refs_in for t, r, _ in rep.refs) or cid in rep.text:
            continue
        words = re.findall(r"[^\W_]{6,}", idx.display_text(unit, unit.leader)[:400].lower())[:12]
        if words and sum(1 for w in set(words) if w in text_lower) >= max(2, len(set(words)) // 3):
            continue
        items.append(Item("consider", f"examined|{cid}",
                          f"you opened {cid} ({_ref(unit.table, unit.leader)}) but the report does not use it. "
                          "Material, or rightly left out?", score=0.35))
    for p in rep.paragraphs:
        n_words = len(p.split())
        if n_words < 120:
            continue
        cites = {m.group(0) for m in re.finditer(r"\b\w+:\d+\b", p)} | {t for t, _ in rep.ids if t in p}
        if len(cites) <= 1:
            items.append(Item("consider", f"thin|{p[:60]}",
                              f'a {n_words}-word passage ("{p.strip()[:60]}…") rests on {len(cites) or "no"} citation'
                              f"{'' if len(cites) == 1 else 's'}. Enough support?", score=0.3))
    return items


def run(idx: Index, report_text: str, dismissed: set[str]) -> tuple[list[Item], list[Item]]:
    years = [p.fields[p.time_field].tmin.year for p in idx.profiles.values()
             if p.time_field and p.fields[p.time_field].tmin]
    year = years[0] if years else date.today().year
    rep = parse_report(idx, report_text, year)
    hay = Haystack.build(idx)
    fixes, considers = fix_checks(idx, rep, hay)
    considers += consider_checks(idx, rep, rep.norm)
    def dedupe(items):
        seen, out = set(), []
        for i in items:
            if i.gid not in seen and i.gid not in dismissed:
                seen.add(i.gid)
                out.append(i)
        return out

    fixes = dedupe(fixes)
    considers = sorted(dedupe(considers), key=lambda i: -i.score)[:MAX_CONSIDER]
    return fixes, considers


def cmd_gapcheck(idx: Index, args) -> str:
    import json
    from pathlib import Path

    from .fmt import footer

    if args.dismiss:
        coverage.record("gapcheck-dismiss", args.dismiss, opened=[])
        return f"dismissed {', '.join(args.dismiss)}; they will not be shown again."
    path = Path(args.report) if args.report else next(
        (p for p in (Path("/work/report.md"), Path("report.md")) if p.exists()), None)
    if path is None or not path.exists():
        return "no report found; pass the path: atlas gapcheck PATH/TO/report.md"
    dismissed = coverage.load_dismissed()
    fixes, considers = run(idx, path.read_text(errors="replace"), dismissed)
    if args.json:
        return json.dumps({"fix": [i.__dict__ for i in fixes], "consider": [i.__dict__ for i in considers]},
                          ensure_ascii=False)
    out = [f"gapcheck {path}: {len(fixes)} to fix, {len(considers)} to consider"]
    out.append("\nFix (claims in the report that the data does not support as written):")
    out += [f"  [{i.gid}] {i.text}" for i in fixes] or ["  none"]
    out.append("\nConsider (optional; include only what is material to your account — leaving out "
               "immaterial items is correct):")
    out += [f"  [{i.gid}] {i.text}" for i in considers] or ["  none"]
    out.append(footer("atlas gapcheck --dismiss gID", "atlas show REF"))
    coverage.record("gapcheck", [str(path)], listed=[])
    return "\n".join(out)
