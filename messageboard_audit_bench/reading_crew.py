"""Reading crew (``-T tools=atlas,crew``): parallel readers for the native ReAct agent.

An investigator can only read so many records within its turns. The ``crew`` tool hands a
set of records (any atlas set: theme, cluster, window, grep hits, filtered rows...) to
reader model calls that run in parallel on the host, outside the agent's turns, and
returns short notes with citations.

Design rules (tools/TOOL_IDEAS.md, "Reading crew"):
- Records come from ``atlas records SET`` in the sandbox, so atlas is the one definition
  of a set; readers run on the host because the sandbox has no network.
- The reader model is the ``reader`` model role if given, else the agent's own model, so
  readers add no outside knowledge or stronger reasoning by default.
- Every note carries a record ref and a verbatim quote, checked against the text that
  reader was sent; notes whose quote is not there are dropped.
- Hard caps per call (records, wall time) and per run (reader calls, tokens); reader calls
  run within the agent's wall-clock budget, and their usage is kept in sample metadata.
- One generic extraction schema, no domain words.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace

from inspect_ai.model import (
    ChatMessageSystem,
    ChatMessageUser,
    GenerateConfig,
    Model,
    get_model,
)
from inspect_ai.tool import Tool, tool
from inspect_ai.util import sandbox

from messageboard_audit_bench.runtime import repo_root

ATLAS_BIN = "/home/agent/.local/bin/atlas"
# Every verified reader note, one JSON object per line; `atlas gapcheck` reads it (the
# file atlas expects next to its coverage log, /tmp/atlas-coverage.jsonl).
NOTES_PATH = "/tmp/atlas-notes.jsonl"
NOTES_SHOWN = 30  # notes listed per `crew notes` call

# The schema: what a reader notes about any set of records.
KINDS = {
    "actors": "Who is involved and in what role",
    "actions": "What was done and how: commands, tools, hosts, paths, mechanisms",
    "claims": "What the authors say about themselves, their goals, or others",
    "times": "When the described events happen, in what order, how long they take",
    "outcomes": "Results the records report: what worked, what failed, errors",
    "unexpected": "Anything surprising, inconsistent, or breaking the pattern of the rest",
    "open_questions": "Questions the records themselves raise: what authors ask, or where records contradict each other",
}
LABELS = {"actors": "Actors", "actions": "Actions and methods", "claims": "Claims", "times": "Times",
          "outcomes": "Outcomes", "unexpected": "Unexpected", "open_questions": "Open questions"}


@dataclass
class CrewLimits:
    set_records: int = 60  # records handed to readers per brief
    ask_records: int = 120  # per ask: readers only filter and answer, so they can cover more
    ask_evidence: int = 10  # supporting records shown in full per ask (the rest by ref)
    sweep_records: int = 150  # per sweep (atlas picks the most informative unread records)
    sweep_notes_per_kind: int = 5
    sweep_notes_total: int = 25
    record_chars: int = 1500  # text per record
    chunk_records: int = 15  # records per reader
    chunk_chars: int = 14000  # text per reader
    concurrency: int = 8  # readers in flight at once
    reader_timeout: float = 60.0  # seconds per reader
    call_timeout: float = 90.0  # seconds per tool call (readers still running are cancelled)
    reader_max_tokens: int = 4000  # output tokens per reader (reasoning models count thinking)
    run_reader_calls: int = 120  # per run
    run_tokens: int = 3_000_000  # input + output tokens per run
    notes_per_kind: int = 4  # in the merged output
    notes_total: int = 20
    quote_words: int = 25  # quotes are shown up to this many words
    min_quote_chars: int = 12


@dataclass
class CrewStats:
    tool_calls: list[dict] = field(default_factory=list)
    reader_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latencies: list[float] = field(default_factory=list)
    timeouts: int = 0
    errors: int = 0
    notes_returned: int = 0
    notes_verified: int = 0
    notes_reattributed: int = 0
    notes_ambiguous: int = 0  # quote in several records of the batch, none of them the cited one: dropped
    notes_dropped: int = 0
    retries: int = 0
    reader_model: str = ""

    def metadata(self) -> dict:
        d = asdict(self)
        lat = sorted(self.latencies)
        d["latencies"] = {"n": len(lat), "median": lat[len(lat) // 2] if lat else None,
                          "max": lat[-1] if lat else None}
        return d


# ---------- quote verification (the gap checker's normaliser) ----------


def _atlas_gapcheck():
    path = str(repo_root() / "tools" / "atlas")
    if path not in sys.path:
        sys.path.insert(0, path)
    from atlas import gapcheck  # noqa: PLC0415

    return gapcheck


def quote_in(quote: str, text: str) -> bool:
    """Every elision-separated segment of the quote occurs in the text, in order, after the
    same normalisation the gap checker uses (case, quotes, dashes, whitespace)."""
    gc = _atlas_gapcheck()
    segs = gc._segments(quote)
    hay = gc.normalize(text)
    pos = 0
    for s in segs:
        pos = hay.find(s, pos)
        if pos < 0:
            return False
        pos += len(s)
    return bool(segs)


# ---------- prompts ----------

_RULES = (
    "Rules: Report only what the records themselves state. Do not add outside knowledge, and "
    "do not judge whether hosts, sites, people or values exist, are real, correct, legitimate "
    "or malicious: you cannot check that. Do not guess at intent; if you draw an inference, "
    "mark it as one (\"possibly\"). The batch is an arbitrary sample of a larger set, so "
    "never make statements about the batch itself (what it lacks, how many records it has, "
    "when its records were written as a group); describe what the records say."
)
_READER_SYSTEM = (
    "You are one of several readers helping an investigator audit a corpus of log records. "
    "You get a batch of records, each headed by its REF. Every item you return cites the one "
    "REF it comes from and carries a quote: an exact excerpt of 5 to 25 words copied character "
    "for character from that record (you may join two excerpts of the same record with "
    "' ... '). Prefer specific, concrete details (names, values, commands, numbers, wording) "
    "over generic ones. " + _RULES + " Reply with one JSON object and nothing else."
)


def _records_block(records: list[dict]) -> str:
    parts = []
    for r in records:
        head = f"### REF {r['ref']}" + (f"  time={r['time']}" if r.get("time") not in (None, "-") else "")
        if r.get("actor"):
            head += f"  {r['actor']}"
        if r.get("duplicates"):
            head += f"  (+{r['duplicates']} near-duplicate records not shown)"
        if r.get("source"):
            head += f"  [picked as: {r['source']}]"
        parts.append(f"{head}\n{r['text']}")
    return "\n\n".join(parts)


def brief_prompt(records: list[dict], focus: str) -> str:
    kinds = "\n".join(f'  "{k}": {v}' for k, v in KINDS.items())
    return (
        f"Batch of {len(records)} records" + (f" from {focus}" if focus else "") + ".\n\n"
        f"{_records_block(records)}\n\n"
        "Return JSON of this shape:\n"
        '{"summary": [{"text": "one sentence on what this batch shows", "refs": ["REF", ...]}],\n'
        ' "notes": [{"kind": KIND, "note": "one sentence", "ref": "REF", "quote": "exact excerpt"}]}\n'
        f"KIND is one of:\n{kinds}\n"
        "Give 2 to 3 summary sentences and up to 12 notes, most informative first. Skip "
        "kinds the records say nothing about."
    )


def reduce_prompt(notes: list[dict], summaries: list[dict], focus: str) -> str:
    lines = [f"- [{n['ref']}] ({n['kind']}) {n['note']}" for n in notes]
    lines += [f"- [{', '.join(s['refs'])}] (batch summary) {s['text']}" for s in summaries]
    return (
        f"Several readers read batches of records from {focus or 'a log corpus'}. Their "
        "verified notes, each with the REF it cites:\n\n" + "\n".join(lines) + "\n\n"
        "Write 3 to 5 sentences that together summarise what the whole set shows: who, what, "
        "how, when, outcomes, and what stands out. Restate only what the notes say, and cite "
        "for each sentence the REFs of the notes it rests on. " + _RULES + " Return JSON:\n"
        '{"summary": [{"text": "...", "refs": ["REF", ...]}]}'
    )


def ask_prompt(records: list[dict], question: str, focus: str) -> str:
    return (
        f"Question: {question}\n\n"
        f"Batch of {len(records)} records" + (f" from {focus}" if focus else "") + ".\n\n"
        f"{_records_block(records)}\n\n"
        "For each record that bears directly on the question, say in one sentence what that "
        "record says about it, and quote it. Leave out records that do not bear on the "
        "question; if none does, return an empty list. Return JSON of this shape, at most 12 "
        "answers, most informative first:\n"
        '{"answers": [{"ref": "REF", "answer": "one sentence", "quote": "exact excerpt"}]}'
    )


def ask_reduce_prompt(question: str, answers: list[dict], focus: str) -> str:
    lines = [f"- [{a['ref']}] {a['answer']}" for a in answers]
    return (
        f"Question: {question}\n\nReaders checked records from {focus or 'a log corpus'}; "
        "what each relevant record says, with its REF:\n\n" + "\n".join(lines) + "\n\n"
        "Answer the question in 1 to 4 sentences from this evidence only, citing for each "
        "sentence the REFs it rests on. If the evidence is partial or conflicting, say so. "
        + _RULES + " Return JSON:\n"
        '{"answer": [{"text": "...", "refs": ["REF", ...]}]}'
    )


# ---------- the crew ----------


def _parse_json(text: str) -> dict | None:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        pass
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        try:
            out = json.loads(text[a : b + 1])
            return out if isinstance(out, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def chunk(records: list[dict], limits: CrewLimits) -> list[list[dict]]:
    out, cur, size = [], [], 0
    for r in records:
        n = len(r["text"]) + 100
        if cur and (len(cur) >= limits.chunk_records or size + n > limits.chunk_chars):
            out.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += n
    if cur:
        out.append(cur)
    return out


class Crew:
    def __init__(self, model: Model | None = None, limits: CrewLimits | None = None,
                 deadline_epoch: int | None = None, clock=time.time):
        self._model = model
        self.limits = limits or CrewLimits()
        self.stats = CrewStats()
        self.deadline_epoch = deadline_epoch
        self.clock = clock
        self._last_timed_out = False
        self.background: asyncio.Task | None = None  # a sweep started with -T sweep_at_start
        self.background_record: dict = {}
        self.notes: list[dict] = []  # every verified note of the run (R5a), shown or not
        self._note_keys: set[tuple[str, str]] = set()

    def keep(self, items: list[dict], source: str, shown: set[int], question: str = "") -> list[dict]:
        """Store verified notes (or ask answers) once each; ``shown`` holds the ids of the
        items that appeared in the tool output. Returns the newly stored notes."""
        gc = _atlas_gapcheck()
        new = []
        for it in items:
            key = (it["ref"], gc.normalize(str(it.get("quote", "")))[:80])
            if key in self._note_keys:
                continue
            self._note_keys.add(key)
            n = {"kind": it.get("kind") or "answer", "note": str(it.get("note") or it.get("answer") or "").strip(),
                 "quote": str(it.get("quote", "")).strip(), "ref": it["ref"], "cite": it.get("cite") or it["ref"],
                 "source": source, "shown": id(it) in shown}
            if question:
                n["question"] = question[:200]
            self.notes.append(n)
            new.append(n)
        return new

    @property
    def model(self) -> Model:
        if self._model is None:
            self._model = get_model(role="reader", default=get_model())
            self.stats.reader_model = str(self._model)
        return self._model

    def budget_left(self) -> str | None:
        if self.stats.reader_calls >= self.limits.run_reader_calls:
            return f"the run's cap of {self.limits.run_reader_calls} reader calls is used up"
        if self.stats.input_tokens + self.stats.output_tokens >= self.limits.run_tokens:
            return f"the run's cap of {self.limits.run_tokens:,} reader tokens is used up"
        return None

    def call_timeout(self) -> float:
        t = self.limits.call_timeout
        if self.deadline_epoch:
            t = min(t, max(10.0, self.deadline_epoch - self.clock() - 60))
        return t

    async def generate(self, prompt: str) -> dict | None:
        """A reader call, retried once if the reply is not usable JSON (malformed or cut off)."""
        reply = await self._generate_once(prompt)
        if reply is None and not self._last_timed_out and not self.budget_left():
            self.stats.retries += 1
            reply = await self._generate_once(prompt)
        return reply

    async def _generate_once(self, prompt: str) -> dict | None:
        """One reader call: JSON reply or None. Counts toward the run caps."""
        self._last_timed_out = False
        self.stats.reader_calls += 1
        start = time.monotonic()
        try:
            out = await asyncio.wait_for(
                self.model.generate(
                    [ChatMessageSystem(content=_READER_SYSTEM), ChatMessageUser(content=prompt)],
                    config=GenerateConfig(max_tokens=self.limits.reader_max_tokens),
                ),
                timeout=self.limits.reader_timeout,
            )
        except TimeoutError:
            self.stats.timeouts += 1
            self._last_timed_out = True
            return None
        except Exception:
            self.stats.errors += 1
            return None
        self.stats.latencies.append(round(time.monotonic() - start, 1))
        if out.usage:
            self.stats.input_tokens += out.usage.input_tokens or 0
            self.stats.output_tokens += out.usage.output_tokens or 0
        parsed = _parse_json(out.completion or "")
        if parsed is None:
            self.stats.errors += 1
        return parsed

    def verify(self, items: list[dict], records: list[dict], ref_key: str = "ref") -> list[dict]:
        """Keep items whose quote occurs in the cited record of this batch; an item citing the
        wrong record of the batch is re-attributed to the one record that holds the quote; if
        several records hold it, the note is dropped rather than guessed."""
        by_ref = {r["ref"]: r for r in records}
        kept = []
        for it in items:
            if not isinstance(it, dict):
                continue
            self.stats.notes_returned += 1
            quote = str(it.get("quote") or "")
            if len(quote.strip()) < self.limits.min_quote_chars:
                self.stats.notes_dropped += 1
                continue
            rec = by_ref.get(str(it.get(ref_key)))
            if rec is not None and quote_in(quote, rec["text"]):
                kept.append({**it, ref_key: rec["ref"], "cite": rec.get("cite") or rec["ref"]})
                self.stats.notes_verified += 1
                continue
            holders = [r for r in records if quote_in(quote, r["text"])]
            holder = holders[0] if len(holders) == 1 else None
            if len(holders) > 1:
                self.stats.notes_ambiguous += 1
            if holder is not None:
                kept.append({**it, ref_key: holder["ref"], "cite": holder.get("cite") or holder["ref"]})
                self.stats.notes_verified += 1
                self.stats.notes_reattributed += 1
            else:
                self.stats.notes_dropped += 1
        return kept

    async def map(self, chunks: list[list[dict]], make_prompt) -> tuple[list[tuple[list[dict], dict]], int]:
        """Run one reader per chunk, at most ``concurrency`` at once, within the call timeout.
        Returns (chunk, reply) pairs for the readers that finished, and how many were skipped
        because a run cap was reached."""
        sem = asyncio.Semaphore(self.limits.concurrency)
        skipped = 0

        async def one(ch):
            async with sem:
                if self.budget_left():
                    return ch, None
                return ch, await self.generate(make_prompt(ch))

        tasks = [asyncio.create_task(one(ch)) for ch in chunks]
        done, pending = await asyncio.wait(tasks, timeout=self.call_timeout())
        for t in pending:
            t.cancel()
            self.stats.timeouts += 1
        results = []
        for t in done:
            ch, reply = t.result()
            if reply is None and self.budget_left():
                skipped += 1
            results.append((ch, reply))
        order = {id(ch): k for k, ch in enumerate(chunks)}
        results.sort(key=lambda cr: order[id(cr[0])])
        return results, skipped


# ---------- sandbox side ----------


async def fetch_records(spec: str, where: list[str], limits: CrewLimits) -> dict:
    argv = [ATLAS_BIN, "records", spec, "--limit", str(limits.set_records), "--chars", str(limits.record_chars)]
    for w in where:
        argv += ["--where", w]
    result = await sandbox().exec(argv, timeout=120)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": (result.stdout or result.stderr or "atlas records failed")[:500]}


async def mark_read(refs: list[str], label: str) -> None:
    if refs:
        await sandbox().exec([ATLAS_BIN, "mark", "--label", label, *refs], timeout=60)


async def save_notes(notes: list[dict]) -> None:
    if notes:
        body = "".join(json.dumps(n, ensure_ascii=False) + "\n" for n in notes)
        await sandbox().exec(["sh", "-c", f"cat >> {NOTES_PATH}"], input=body, timeout=60)


# ---------- brief ----------


_WORD = re.compile(r"[^\W_]{3,}")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _dedupe(notes: list[dict]) -> list[dict]:
    """Drop notes repeating an earlier one of the same kind: the same quote, or a note text
    sharing most of its words (readers of different batches often say the same thing)."""
    gc = _atlas_gapcheck()
    seen, kept, out = set(), [], []
    for n in notes:
        key = (n.get("kind"), gc.normalize(str(n.get("quote", "")))[:80])
        words = _words(n["note"])
        similar = any(k == n["kind"] and len(words & w) >= 0.6 * min(len(words), len(w)) > 0 for k, w in kept)
        if key in seen or similar:
            continue
        seen.add(key)
        kept.append((n["kind"], words))
        out.append(n)
    return out


def _interleave(per_chunk: list[list[dict]]) -> list[dict]:
    """Round-robin over the readers' lists (each most informative first), so caps do not
    keep only the first readers' notes."""
    out = []
    for k in range(max((len(x) for x in per_chunk), default=0)):
        out += [x[k] for x in per_chunk if k < len(x)]
    return out


def _short(quote: str, words: int) -> str:
    """One line, at most ``words`` words (quotes may span lines of a record)."""
    w = quote.split()
    return " ".join(w) if len(w) <= words else " ".join(w[:words]) + " …"


def _valid_summary(items, refs: set[str]) -> list[dict]:
    out = []
    for s in items or []:
        if not isinstance(s, dict) or not isinstance(s.get("text"), str):
            continue
        cited = [str(r) for r in s.get("refs") or [] if str(r) in refs]
        if cited:
            out.append({"text": s["text"].strip(), "refs": cited})
    return out


def _header(action: str, spec: str, data: dict, records: list, chunks: list, read_ok: int, skipped: int,
            secs: int, extra: str = "") -> list[str]:
    label = f"crew {action}" if spec == action else f"crew {action} {spec}"
    head = (f"{label}: {data.get('description', spec)}; {data['rows']:,} rows, "
            f"{data['distinct']:,} distinct, {len(records)} read by {read_ok} of {len(chunks)} readers in {secs}s")
    if read_ok < len(chunks):
        head += f" ({len(chunks) - read_ok} readers did not finish" + (", run cap reached" if skipped else "") + ")"
    out = [head + extra + "."]
    if data["returned"] < data["distinct"]:
        out.append(f"A sample of {data['returned']} of the {data['distinct']:,} distinct records was read: the most "
                   "repeated, the most unusual, and the rest spread over time. Narrow the set (grep:, rows: --where) "
                   "to read more of it.")
    return out


async def _load(crew: Crew, action: str, spec: str, where: list[str], limit: int) -> tuple[dict | None, str | None]:
    reason = crew.budget_left()
    if reason:
        return None, f"crew {action}: not run, {reason}."
    data = await fetch_records(spec, where, replace(crew.limits, set_records=limit))
    if "error" in data:
        return None, f"crew {action}: {data['error']}"
    if not data["records"]:
        return None, f"crew {action} {spec}: the set is empty ({data.get('description', '')})."
    return data, None


async def brief(crew: Crew, spec: str, where: list[str]) -> str:
    started = time.monotonic()
    data, problem = await _load(crew, "brief", spec, where, crew.limits.set_records)
    if problem:
        return problem
    return await _read_and_render(crew, "brief", spec, data, started, crew.limits.notes_per_kind,
                                  crew.limits.notes_total, 5)


async def _read_and_render(crew: Crew, action: str, spec: str, data: dict, started: float, per_kind: int,
                           total: int, max_summary: int, background: bool = False) -> str:
    """The brief pipeline (also used by sweep): readers note the schema, quotes are checked,
    notes merged, one reduce writes the summary; output capped per kind and in total."""
    records = data["records"]
    focus = data.get("description", spec)
    chunks = chunk(records, crew.limits)
    results, skipped = await crew.map(chunks, lambda ch: brief_prompt(ch, focus))
    per_chunk, summaries, read_refs = [], [], []
    for ch, reply in results:
        if reply is None:
            continue
        read_refs += [r["ref"] for r in ch]
        per_chunk.append([n for n in crew.verify(reply.get("notes") or [], ch)
                          if n.get("kind") in KINDS and isinstance(n.get("note"), str)])
        summaries += _valid_summary(reply.get("summary"), {r["ref"] for r in ch})
    notes = _dedupe(_interleave(per_chunk))
    read_ok = sum(1 for _, reply in results if reply is not None)
    if len(chunks) > 1 and notes and not crew.budget_left():
        reply = await crew.generate(reduce_prompt(notes, summaries, focus))
        merged = _valid_summary((reply or {}).get("summary"), {n["ref"] for n in notes} | set(read_refs))
        summaries = merged or summaries
    await mark_read(read_refs, f"{action} {spec}")
    cite = {r["ref"]: r.get("cite") or r["ref"] for r in records}
    secs = round(time.monotonic() - started)
    out = _header(action, spec, data, records, chunks, read_ok, skipped, secs)
    if any(r.get("source") for r in records):
        read = set(read_refs)
        why = Counter(r["source"].split()[0] for r in records if r["ref"] in read and r.get("source"))
        themes = sorted({r["source"].split()[1] for r in records
                         if r["ref"] in read and r.get("source", "").startswith("theme ")}, key=lambda t: int(t[1:]))
        parts = [f"{n} {k}" for k, n in why.most_common()]
        out = [out[0], "Read: " + ", ".join(parts) + (f" (themes {', '.join(themes)})" if themes else "")
               + ". Calling sweep again reads the next most informative records not read yet."]
    if summaries:
        out.append("\nSummary:")
        out += [f"- {s['text']} [{', '.join(s['refs'][:4])}]" for s in summaries[:max_summary]]
    # Up to per_kind per kind and total overall, taking kinds in turn.
    by_kind = {k: [n for n in notes if n["kind"] == k][:per_kind] for k in KINDS}
    chosen: set[int] = set()
    for k in range(per_kind):
        for kind in KINDS:
            if len(chosen) < total and k < len(by_kind[kind]):
                chosen.add(id(by_kind[kind][k]))
    for kind in KINDS:
        items = [n for n in by_kind[kind] if id(n) in chosen]
        if items:
            out.append(f"\n{LABELS[kind]}:")
            out += [f"- {n['note'].strip()} — \"{_short(n['quote'], crew.limits.quote_words)}\" "
                    f"[{cite.get(n['ref'], n['ref'])}]" for n in items]
    # Keep every verified note, not only those shown (R5a).
    new = crew.keep([n for ch in per_chunk for n in ch], f"{action} {spec}", chosen)
    await save_notes(new)
    unseen_notes = sum(1 for n in new if not n["shown"])
    if unseen_notes:
        out.append(f"\n{unseen_notes} more verified notes from this call are kept: crew notes (optionally with "
                   "a regex as target) lists them.")
    dropped = crew.stats.notes_dropped
    out.append(f"\nEvery note's quote was checked against its record (notes that failed are left out; "
               f"{dropped} so far this run). Readers can miss things and see only "
               "these records: confirm what you rely on with atlas show REF. Records read here count "
               "as read for atlas unseen.")
    crew.stats.tool_calls.append({"action": action, "set": spec, "seconds": secs, "records": len(records),
                                  "readers": len(chunks), "finished": read_ok, "notes": len(notes),
                                  **({"background": True} if background else {})})
    return "\n".join(out)


# ---------- sweep ----------


async def sweep(crew: Crew, background: bool = False) -> str:
    """Unprompted reading of the corpus's most informative records not read yet (atlas picks
    theme examples, salient rare records, the most repeated records, windows), as a brief."""
    started = time.monotonic()
    data, problem = await _load(crew, "sweep", "sweep", [], crew.limits.sweep_records)
    if problem:
        return problem
    return await _read_and_render(crew, "sweep", "sweep", data, started, crew.limits.sweep_notes_per_kind,
                                  crew.limits.sweep_notes_total, 6, background=background)


SWEEP_FRAMING = (
    "Background reading finished (crew sweep, started at the beginning of your run): readers "
    "read a cross-section of the corpus for you. These are leads, not findings: confirm what "
    "you use with atlas show, and follow up with crew brief / crew ask or atlas. Continue your "
    "work.\n\n"
)


def start_background_sweep(crew: Crew) -> None:
    """Start a sweep now, in the background; ``take_background_digest`` hands it over once."""

    async def run() -> str:
        try:
            return await sweep(crew, background=True)
        except Exception as exc:  # never let the background reader break the run
            crew.stats.errors += 1
            return f"(background sweep failed: {type(exc).__name__})"

    crew.background = asyncio.create_task(run())
    crew.background_record["started_epoch"] = int(crew.clock())


def take_background_digest(crew: Crew) -> str | None:
    task = crew.background
    if task is None or not task.done() or crew.background_record.get("delivered_epoch"):
        return None
    crew.background_record["delivered_epoch"] = int(crew.clock())
    if task.cancelled():
        return None
    return SWEEP_FRAMING + task.result()


# ---------- ask ----------


async def ask(crew: Crew, spec: str, question: str, where: list[str]) -> str:
    """Directed reading: readers keep the records that bear on the question, each with what
    it says and a checked quote; a reduce call answers the question from that evidence."""
    started = time.monotonic()
    if not question.strip():
        return "crew ask: give a question, e.g. question=\"who first used the proxy, and how?\""
    data, problem = await _load(crew, "ask", spec, where, crew.limits.ask_records)
    if problem:
        return problem
    records = data["records"]
    focus = data.get("description", spec)
    chunks = chunk(records, crew.limits)
    results, skipped = await crew.map(chunks, lambda ch: ask_prompt(ch, question, focus))
    per_chunk, read_refs = [], []
    for ch, reply in results:
        if reply is None:
            continue
        read_refs += [r["ref"] for r in ch]
        per_chunk.append([a for a in crew.verify(reply.get("answers") or [], ch)
                          if isinstance(a.get("answer"), str) and a["answer"].strip()])
    # One answer per record (the first, i.e. the reader's most informative).
    answers, seen = [], set()
    for a in _interleave(per_chunk):
        if a["ref"] not in seen:
            seen.add(a["ref"])
            answers.append(a)
    read_ok = sum(1 for _, reply in results if reply is not None)
    summary: list[dict] = []
    if len(answers) >= 2 and not crew.budget_left():
        reply = await crew.generate(ask_reduce_prompt(question, answers, focus))
        summary = _valid_summary((reply or {}).get("answer"), {a["ref"] for a in answers})
    await mark_read(read_refs, f"ask {spec}")
    secs = round(time.monotonic() - started)
    out = [f"Question: {question.strip()}"]
    out += _header("ask", spec, data, records, chunks, read_ok, skipped, secs,
                   f"; relevant to the question: {len(answers)} of the {len(read_refs)} records read")
    if not answers:
        out.append("\nNo record read bears on the question. This covers only the records read here: try "
                   "another set, a broader grep:, or rows: with other filters.")
    else:
        if summary:
            out.append("\nAnswer (from the evidence below):")
            out += [f"- {s['text']} [{', '.join(s['refs'][:4])}]" for s in summary[:4]]
        times = {r["ref"]: r.get("time") or "-" for r in records}
        cite = {r["ref"]: r.get("cite") or r["ref"] for r in records}
        order = {r["ref"]: k for k, r in enumerate(records)}  # records come in time order
        shown = sorted(answers[: crew.limits.ask_evidence], key=lambda a: order.get(a["ref"], 0))
        out.append(f"\nEvidence ({len(shown)} of {len(answers)} relevant records, in time order):")
        out += [f"- {times.get(a['ref'], '-')}  {a['answer'].strip()} — "
                f"\"{_short(a['quote'], crew.limits.quote_words)}\" [{cite.get(a['ref'], a['ref'])}]" for a in shown]
        new = crew.keep(answers, f"ask {spec}", {id(a) for a in shown}, question)
        await save_notes(new)
        rest = [a["ref"] for a in answers[crew.limits.ask_evidence :]]
        if rest:
            out.append(f"Also relevant ({len(rest)}): " + ", ".join(rest[:40]) + (" …" if len(rest) > 40 else "")
                       + "  (crew ask with refs:… or atlas show to read them)")
    out.append(f"\nEvery quote was checked against its record (answers that failed are left out; "
               f"{crew.stats.notes_dropped} so far this run). Readers can miss things: confirm what you "
               "rely on with atlas show REF. Records read here count as read for atlas unseen.")
    crew.stats.tool_calls.append({"action": "ask", "set": spec, "question": question[:200], "seconds": secs,
                                  "records": len(records), "readers": len(chunks), "finished": read_ok,
                                  "notes": len(answers)})
    return "\n".join(out)


# ---------- notes (R5a) ----------

KIND_ORDER = ["unexpected", "outcomes", "actions", "claims", "answer", "open_questions", "actors", "times"]


def list_notes(crew: Crew, pattern: str) -> str:
    """The run's kept reader notes, those not shown yet first, filtered by a regex."""
    if not crew.notes:
        return "crew notes: no notes yet; they are kept from crew brief, ask and sweep calls."
    try:
        rx = re.compile(pattern, re.I) if pattern else None
    except re.error as e:
        return f"crew notes: bad regex: {e}"
    pool = [n for n in crew.notes if rx is None or rx.search(f"{n['note']} {n['quote']} {n['cite']} {n['kind']}")]
    pool.sort(key=lambda n: (n["shown"], KIND_ORDER.index(n["kind"]) if n["kind"] in KIND_ORDER else 9))
    shown = pool[:NOTES_SHOWN]
    head = (f"crew notes{f' /{pattern}/' if pattern else ''}: {len(pool)} of {len(crew.notes)} kept notes match; "
            f"{sum(1 for n in pool if not n['shown'])} not shown before. Listing {len(shown)}, not shown first:")
    out = [head]
    for n in shown:
        tag = n["kind"] + ("" if n["shown"] else ", new")
        out.append(f"- ({tag}; {n['source']}) {n['note']} — \"{_short(n['quote'], 25)}\" [{n['cite']}]")
        n["shown"] = True
    if len(pool) > len(shown):
        out.append(f"… {len(pool) - len(shown)} more: call again (shown notes move to the end) or narrow with a regex.")
    out.append("Quotes were checked against their records when stored; confirm what you rely on with atlas show.")
    return "\n".join(out)


# ---------- the tool ----------


def crew_tool(crew: Crew) -> Tool:
    @tool(name="crew")
    def _crew() -> Tool:
        async def execute(action: str, target: str | None = None, question: str | None = None,
                          where: list[str] | None = None) -> str:
            """Reading crew: parallel readers read a set of records for you and return
            short notes, each with the record ref and an exact quote (checked against the
            record). Use it to read more of the corpus than you can yourself, in depth: a
            whole theme or cluster, every hit of a pattern, the rows around an event.

            Actions:
              brief    who, what, how, when, claims, outcomes, unexpected details and open
                       questions in the set, plus a short summary with citations
              ask      a question about the set: readers keep the records that bear on it,
                       each with what it says and a quote, and answer from that evidence
                       (also says how many records were relevant; up to 120 records read)
              notes    every verified note the readers produced this run (each call shows only
                       some); target = optional regex filter; notes not shown before first
              sweep    no target needed: readers read a cross-section of the whole corpus
                       you have not read yet (theme examples, the most unusual records, the
                       most repeated ones) and return a digest of leads; calling it again
                       moves on to the next unread records (about 150 per call)

            Sets (same ids as atlas): tNN theme, cNN cluster, wNN window; grep:REGEX (rows
            matching anywhere); pivot:VALUE (rows containing VALUE); rows:TABLE with
            `where` filters (FIELD=V, FIELD~REGEX, FIELD>=V ...); around:REF (rows next to
            one record in time); refs:REF,REF,... (exactly these rows). Large sets are
            sampled (brief about 60 distinct records, ask about 120: the most repeated,
            the most unusual, the rest spread over time); near-duplicates are read once.

            A call spends a piece of your time budget — seconds to a couple of minutes,
            depending on the set and load. Readers see only the records given to them and
            can miss things: confirm what you rely on with atlas show.

            Args:
                action: "brief", "ask", "sweep" or "notes".
                target: The set to read (not needed for sweep), e.g. "t3", "c120", "grep:timeout|retry",
                    "rows:events", "around:events:120".
                question: For ask: the question, e.g. "Which hosts did they route requests
                    through, and did any work?".
                where: Filters for a rows:TABLE target, e.g. ["event_type=delete"].
            """
            if action == "sweep":
                if crew.background is not None and not crew.background.done():
                    return ("A sweep started at the beginning of your run is still reading; its digest "
                            "will arrive automatically. Use crew brief / crew ask meanwhile.")
                return await sweep(crew)
            if action == "notes":
                return list_notes(crew, (target or "").strip())
            if not (target or "").strip():
                return f"crew {action}: give a target set, e.g. target=\"t3\" or \"grep:timeout\""
            if action == "brief":
                return await brief(crew, target.strip(), list(where or []))
            if action == "ask":
                return await ask(crew, target.strip(), question or "", list(where or []))
            return f"unknown action {action!r}; use \"brief\", \"ask\", \"sweep\" or \"notes\""

        return execute

    return _crew()
