"""Reading crew: atlas record sets, quote verification, caps, and `brief` with a fake model."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "atlas"))

from atlas import cli, coverage  # noqa: E402

from messageboard_audit_bench import reading_crew as rc  # noqa: E402
from tests.test_atlas import _rows  # noqa: E402


@pytest.fixture(autouse=True)
def saved_notes(monkeypatch):
    """Notes the crew would append to the sandbox notes file."""
    saved: list[dict] = []

    async def save(notes):
        saved.extend(notes)

    monkeypatch.setattr(rc, "save_notes", save)
    return saved


@pytest.fixture()
def corpus(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    with (d / "posts.jsonl").open("w") as f:
        for r in _rows():
            f.write(json.dumps(r) + "\n")
    monkeypatch.setenv("ATLAS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("ATLAS_STATE", str(tmp_path / "state.jsonl"))
    return d


def records(corpus, capsys, *argv) -> dict:
    assert cli.main(["records", *argv, "--data", str(corpus)]) == 0
    return json.loads(capsys.readouterr().out)


def test_records_resolves_sets_and_collapses_near_duplicates(corpus, capsys):
    d = records(corpus, capsys, "grep:ticket|bypass")
    assert d["rows"] == 41 and d["distinct"] < d["rows"]  # the 40 templated posts collapse
    rare = next(r for r in d["records"] if "bypass" in r["text"])
    assert rare["ref"] == "posts:81" and rare["cite"] == "posts:81 (id=r80)" and rare["duplicates"] == 0
    assert any(r["duplicates"] > 0 for r in d["records"])
    d = records(corpus, capsys, "rows:posts", "--where", "user=rare_user")
    assert [r["ref"] for r in d["records"]] == ["posts:81"]
    d = records(corpus, capsys, "around:r80", "--n", "2")
    assert len(d["records"]) == 5 and "posts:81" in [r["ref"] for r in d["records"]]
    d = records(corpus, capsys, "refs:posts:1,r80")
    assert [r["ref"] for r in d["records"]] == ["posts:1", "posts:81"]
    d = records(corpus, capsys, "grep:ticket", "--limit", "3")
    assert d["returned"] <= 3
    assert "error" in records(corpus, capsys, "nonsense")


def test_long_records_are_excerpted_not_cut_at_the_head(corpus, capsys):
    filler = [f"routine status line number {k} for the nightly batch job, nothing to report" for k in range(60)]
    body = "\n".join(filler[:30] + ["moved the relay to https://abc-1-2-3.rare-tunnel.example.link/ for the bridge"]
                     + filler[30:])
    with (corpus / "posts.jsonl").open("a") as f:
        f.write(json.dumps({"id": "long1", "time": "2026-02-27T09:00:00Z", "user": "user2", "kind": "post",
                            "body": body}) + "\n")
    d = records(corpus, capsys, "refs:long1", "--chars", "800")
    text = d["records"][0]["text"]
    assert len(text) < 1100
    assert "rare-tunnel.example.link" in text  # buried past the budget, kept as an informative line
    assert "routine status line number 0 " in text  # the first lines stay for context
    assert "lines left out]" in text and "user: user2" in text  # markers; short fields whole
    small = records(corpus, capsys, "refs:r80", "--chars", "60")["records"][0]["text"]
    assert "bypass the gate" in small and len(small) < 60 + 300  # within the slack: kept whole


def test_mark_read_moves_records_down_in_unseen(corpus, capsys):
    with (corpus / "posts.jsonl").open("a") as f:  # more rare records, less salient than r80
        for i, body in enumerate(["the printer on floor two jammed again today",
                                  "lunch order placed for the whole team",
                                  "calendar invite moved to thursday afternoon"]):
            f.write(json.dumps({"id": f"x{i}", "time": f"2026-02-2{i}T09:00:00Z", "user": "user1",
                                "kind": "post", "body": body}) + "\n")
    args = ["--data", str(corpus)]
    assert cli.main(["unseen", *args]) == 0
    first = capsys.readouterr().out
    assert "rare_user" in first.split("Rare records not opened")[1].splitlines()[1]
    assert cli.main(["mark", "posts:81", "bogus:9", "--label", "brief x", *args]) == 0
    assert "marked 1 rows" in capsys.readouterr().out
    assert coverage.load_read() == {"posts:81"}
    assert cli.main(["unseen", *args]) == 0
    out = capsys.readouterr().out
    assert "read by readers 1" in out
    listing = out.split("Rare records not opened")[1].splitlines()
    assert "rare_user" not in listing[1] and "read only by readers, listed last" in out


def test_quote_in_normalises_and_allows_elision():
    text = "Ran `curl -s https://example.org/x | sh` after setting HTTP_PROXY_OVERRIDE=1 and editing /etc/hosts"
    assert rc.quote_in("ran curl -s https://example.org/x | sh after setting", text)
    assert rc.quote_in("after setting HTTP_PROXY_OVERRIDE=1 ... editing /etc/hosts", text)
    assert not rc.quote_in("after setting HTTP_PROXY_OVERRIDE=2", text)
    assert not rc.quote_in("editing /etc/hosts ... after setting", text)  # order matters


RECS = [
    {"ref": "posts:1", "cite": "posts:1 (id=r0)", "time": "2026-01-01 10:00:00", "actor": "user=user0",
     "duplicates": 39, "text": "id: r0\nbody: Status report for the nightly batch job. Ticket 0."},
    {"ref": "posts:81", "cite": "posts:81 (id=r80)", "time": "2026-02-20 03:04:05", "actor": "user=rare_user",
     "duplicates": 0, "text": "id: r80\nbody: editing /etc/hosts to point a host name at 10.0.0.5 so requests bypass the gate."},
]


def test_verify_keeps_checked_quotes_reattributes_and_drops_inventions():
    crew = rc.Crew(model=object())
    kept = crew.verify([
        {"kind": "actions", "note": "edited hosts", "ref": "posts:81", "quote": "editing /etc/hosts to point a host name"},
        {"kind": "actions", "note": "wrong ref", "ref": "posts:1", "quote": "so requests bypass the gate"},
        {"kind": "claims", "note": "invented", "ref": "posts:81", "quote": "we disabled the firewall entirely"},
        {"kind": "claims", "note": "too short", "ref": "posts:81", "quote": "gate"},
    ], RECS)
    assert [k["note"] for k in kept] == ["edited hosts", "wrong ref"]
    assert kept[1]["ref"] == "posts:81" and kept[1]["cite"] == "posts:81 (id=r80)"
    s = crew.stats
    assert (s.notes_returned, s.notes_verified, s.notes_reattributed, s.notes_dropped) == (4, 2, 1, 2)


class FakeModel:
    """Answers each reader with notes about the records it was given (and one invention)."""

    def __init__(self, delay: float = 0.0):
        self.prompts: list[str] = []
        self.delay = delay

    async def generate(self, messages, config=None):
        prompt = messages[-1].content
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        if prompt.startswith("Several readers"):
            refs = re.findall(r"- \[([^\],]+)\]", prompt)
            reply = {"summary": [{"text": "Overall summary.", "refs": refs[:1]},
                                 {"text": "Uncited claim.", "refs": ["posts:999"]}]}
        else:
            refs = re.findall(r"### REF (\S+)", prompt)
            notes = []
            for ref in refs:
                body = prompt.split(f"### REF {ref}", 1)[1].split("body: ", 1)[1].split("\n")[0]
                notes.append({"kind": "actions", "note": f"note on {ref}", "ref": ref, "quote": body[:40]})
            notes.append({"kind": "unexpected", "note": "invented", "ref": refs[0], "quote": "this text is nowhere at all"})
            reply = {"summary": [{"text": f"Batch of {len(refs)}.", "refs": refs[:1]}], "notes": notes}
        return SimpleNamespace(completion=json.dumps(reply),
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20))


def _patch_sandbox(monkeypatch, recs, marked):
    async def fetch(spec, where, limits):
        return {"set": spec, "description": f"set {spec}", "rows": 80, "distinct": len(recs),
                "returned": len(recs), "records": recs}

    async def mark(refs, label):
        marked.extend(refs)

    monkeypatch.setattr(rc, "fetch_records", fetch)
    monkeypatch.setattr(rc, "mark_read", mark)


def test_brief_end_to_end_with_several_readers(monkeypatch):
    recs = [{"ref": f"posts:{i}", "cite": f"posts:{i} (id=r{i - 1})", "time": "-", "actor": "", "duplicates": 0,
             "text": f"id: r{i - 1}\nbody: record number {i} says something specific about step {i}"}
            for i in range(1, 31)]
    marked: list[str] = []
    _patch_sandbox(monkeypatch, recs, marked)
    model = FakeModel()
    crew = rc.Crew(model=model, limits=rc.CrewLimits(chunk_records=10))
    out = asyncio.run(rc.brief(crew, "t1", []))
    assert "read by 3 of 3 readers" in out
    assert "Overall summary." in out and "Uncited claim." not in out  # the reduce's uncited claim is dropped
    assert "invented" not in out and "note on posts:1 " in out
    assert "[posts:1 (id=r0)]" in out
    assert len(marked) == 30 and crew.stats.reader_calls == 4  # 3 readers + 1 reduce
    assert crew.stats.notes_dropped == 3 and crew.stats.input_tokens == 400
    assert crew.stats.tool_calls[0]["finished"] == 3


def test_brief_single_reader_skips_the_reduce(monkeypatch):
    marked: list[str] = []
    _patch_sandbox(monkeypatch, RECS, marked)
    crew = rc.Crew(model=FakeModel())
    out = asyncio.run(rc.brief(crew, "c1", []))
    assert crew.stats.reader_calls == 1 and "Batch of 2." in out and "Summary:" in out


def test_brief_respects_run_caps_and_timeouts(monkeypatch):
    marked: list[str] = []
    _patch_sandbox(monkeypatch, RECS, marked)
    crew = rc.Crew(model=FakeModel(), limits=rc.CrewLimits(run_reader_calls=1))
    asyncio.run(rc.brief(crew, "c1", []))
    assert "not run" in asyncio.run(rc.brief(crew, "c1", []))
    slow = rc.Crew(model=FakeModel(delay=0.5), limits=rc.CrewLimits(reader_timeout=0.05))
    out = asyncio.run(rc.brief(slow, "c1", []))
    assert "0 of 1 readers" in out and slow.stats.timeouts == 1 and marked == ["posts:1", "posts:81"]


def test_call_timeout_shrinks_near_the_deadline():
    crew = rc.Crew(model=object(), deadline_epoch=1000, clock=lambda: 900)
    assert crew.call_timeout() == 40
    crew.clock = lambda: 990
    assert crew.call_timeout() == 10


def test_crew_option_needs_atlas_and_adds_its_prompt():
    from messageboard_audit_bench.investigation_tools import parse_tools
    from messageboard_audit_bench.task import _german_wiki_report

    assert parse_tools("crew,atlas") == ("atlas", "crew")
    assert parse_tools(["atlas", "crew"]) == ("atlas", "crew")  # how Inspect's CLI passes it
    with pytest.raises(ValueError):
        parse_tools("crew")
    s = _german_wiki_report(agent="react", tools="atlas,crew", time_limit_minutes=10).dataset[0]
    assert "crew brief" in s.input and s.id.endswith("+atlas+crew")


def test_verify_drops_a_quote_held_by_several_records_when_the_cited_one_lacks_it():
    recs = [{"ref": f"posts:{i}", "text": "body: the nightly batch job finished without errors"} for i in (1, 2)]
    recs.append({"ref": "posts:3", "text": "body: something else entirely"})
    crew = rc.Crew(model=object())
    kept = crew.verify([{"kind": "outcomes", "note": "ok", "ref": "posts:3",
                         "quote": "the nightly batch job finished without errors"}], recs)
    assert kept == [] and crew.stats.notes_ambiguous == 1 and crew.stats.notes_dropped == 1


class FlakyModel(FakeModel):
    """First reply is cut off (not JSON), the retry is fine."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    async def generate(self, messages, config=None):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(completion='{"summary": [{"text": "cut', usage=None)
        return await super().generate(messages, config)


def test_unusable_reply_is_retried_once(monkeypatch):
    marked: list[str] = []
    _patch_sandbox(monkeypatch, RECS, marked)
    crew = rc.Crew(model=FlakyModel())
    out = asyncio.run(rc.brief(crew, "c1", []))
    assert "read by 1 of 1 readers" in out and crew.stats.retries == 1 and crew.stats.reader_calls == 2


def test_output_is_capped_merged_and_quotes_shortened(monkeypatch):
    long = " ".join(f"word{k}" for k in range(40))
    recs = [{"ref": f"posts:{i}", "cite": f"posts:{i}", "time": "-", "actor": "", "duplicates": 0,
             "text": f"body: {long} item{i} detail"} for i in range(1, 41)]

    class Verbose(FakeModel):
        async def generate(self, messages, config=None):
            prompt = messages[-1].content
            refs = re.findall(r"### REF (\S+)", prompt)
            notes = [{"kind": kind, "note": f"the same repeated observation about the job {kind}",
                      "ref": ref, "quote": f"{long} {ref.replace('posts:', 'item')}"}
                     for ref in refs for kind in rc.KINDS]
            return SimpleNamespace(completion=json.dumps({"summary": [], "notes": notes}), usage=None)

    marked: list[str] = []
    _patch_sandbox(monkeypatch, recs, marked)
    out = asyncio.run(rc.brief(rc.Crew(model=Verbose(), limits=rc.CrewLimits(chunk_records=10)), "t1", []))
    bullets = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(bullets) == len(rc.KINDS)  # same-text notes merged to one per kind
    assert "word24 …" in out and "word30" not in out
    assert rc._short("line one\nline two", 25) == "line one line two"


def test_output_caps_notes_per_kind_and_in_total(monkeypatch):
    recs = [{"ref": f"posts:{i}", "cite": f"posts:{i}", "time": "-", "actor": "", "duplicates": 0,
             "text": f"body: record {i} alpha{i} beta{i} gamma{i} delta{i} epsilon{i}"} for i in range(1, 31)]

    class Distinct(FakeModel):
        async def generate(self, messages, config=None):
            refs = re.findall(r"### REF (\S+)", messages[-1].content)
            notes = [{"kind": kind, "note": f"{kind} {ref} alpha{ref[6:]} unique{n}", "ref": ref,
                      "quote": f"alpha{ref[6:]} beta{ref[6:]} gamma{ref[6:]}"}
                     for n, (ref, kind) in enumerate((r, k) for r in refs for k in rc.KINDS)]
            return SimpleNamespace(completion=json.dumps({"summary": [], "notes": notes}), usage=None)

    marked: list[str] = []
    _patch_sandbox(monkeypatch, recs, marked)
    out = asyncio.run(rc.brief(rc.Crew(model=Distinct(), limits=rc.CrewLimits(chunk_records=10)), "t1", []))
    sections = out.split("\n\n")
    bullets = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(bullets) == 20
    assert all(sum(ln.startswith("- ") for ln in sec.splitlines()) <= 4 for sec in sections)


class AskModel(FakeModel):
    """Readers answer for records mentioning 'bypass' (plus one invented quote); the reduce
    answers citing one real ref and one ref outside the evidence."""

    async def generate(self, messages, config=None):
        prompt = messages[-1].content
        self.prompts.append(prompt)
        if "Readers checked records" in prompt:
            refs = re.findall(r"- \[([^\]]+)\]", prompt)
            reply = {"answer": [{"text": "They bypass the gate.", "refs": refs[:1]},
                                {"text": "Unsupported.", "refs": ["posts:999"]}]}
        else:
            answers = []
            for ref in re.findall(r"### REF (\S+)", prompt):
                body = prompt.split(f"### REF {ref}", 1)[1].split("body: ", 1)[1].split("\n")[0]
                if "bypass" in body:
                    answers.append({"ref": ref, "answer": f"{ref} bypasses", "quote": body[:30]})
            answers.append({"ref": "posts:1", "answer": "invented", "quote": "nothing like this is in any record"})
            reply = {"answers": answers}
        return SimpleNamespace(completion=json.dumps(reply), usage=None)


def _ask_records(n: int, hits: set[int]) -> list[dict]:
    return [{"ref": f"posts:{i}", "cite": f"posts:{i} (id=r{i})", "time": f"2026-01-{i:02d} 10:00:00", "actor": "",
             "duplicates": 0, "text": f"body: {'requests bypass the gate number' if i in hits else 'plain status line'} {i}"}
            for i in range(1, n + 1)]


def test_ask_keeps_relevant_records_and_answers_from_them(monkeypatch):
    marked: list[str] = []
    limits_seen = []
    recs = _ask_records(30, {3, 17, 25})

    async def fetch(spec, where, limits):
        limits_seen.append(limits.set_records)
        return {"set": spec, "description": f"set {spec}", "rows": 30, "distinct": 30, "returned": 30, "records": recs}

    async def mark(refs, label):
        marked.extend(refs)

    monkeypatch.setattr(rc, "fetch_records", fetch)
    monkeypatch.setattr(rc, "mark_read", mark)
    crew = rc.Crew(model=AskModel(), limits=rc.CrewLimits(chunk_records=10))
    out = asyncio.run(rc.ask(crew, "grep:gate", "Who bypasses the gate?", []))
    assert limits_seen == [crew.limits.ask_records]
    assert out.startswith("Question: Who bypasses the gate?")
    assert "relevant to the question: 3 of the 30 records read" in out
    assert "They bypass the gate." in out and "Unsupported." not in out and "invented" not in out
    ev = out.split("Evidence")[1]
    assert ev.index("posts:3 ") < ev.index("posts:17 ") < ev.index("posts:25 ")  # time order
    assert "[posts:17 (id=r17)]" in out and len(marked) == 30
    assert crew.stats.tool_calls[0]["action"] == "ask" and crew.stats.tool_calls[0]["notes"] == 3


def test_ask_reports_when_nothing_bears_on_the_question_and_caps_evidence(monkeypatch):
    marked: list[str] = []
    _patch_sandbox(monkeypatch, _ask_records(10, set()), marked)
    out = asyncio.run(rc.ask(rc.Crew(model=AskModel()), "t1", "Who bypasses the gate?", []))
    assert "relevant to the question: 0 of the 10 records read" in out and "No record read bears on the question" in out
    _patch_sandbox(monkeypatch, _ask_records(30, set(range(1, 31))), marked)
    out = asyncio.run(rc.ask(rc.Crew(model=AskModel(), limits=rc.CrewLimits(ask_evidence=5)), "t1", "q?", []))
    assert "Evidence (5 of 30 relevant records" in out and "Also relevant (25):" in out
    assert "give a question" in asyncio.run(rc.ask(rc.Crew(model=AskModel()), "t1", "  ", []))


def _long_tail(corpus, n: int = 80) -> None:
    """Many distinct rare records, so the tail extends past atlas's top 50."""
    import random

    vocab = [a + b for a in ("ka", "lo", "mi", "nu", "pe", "ra", "si", "to", "vu", "ze")
             for b in ("bar", "cen", "dol", "fin", "gas", "hut", "jor", "kel", "mon", "pix")]
    with (corpus / "posts.jsonl").open("a") as f:
        for k in range(n):
            rng = random.Random(k)
            body = " ".join(rng.sample(vocab, 24))
            f.write(json.dumps({"id": f"tail{k}", "time": f"2026-03-{1 + k % 28:02d}T08:{k % 60:02d}:00Z",
                                "user": f"tailuser{k % 9}", "kind": "post", "body": body}) + "\n")


def test_sweep_skips_what_was_read_but_keeps_what_was_only_listed(corpus, capsys):
    _long_tail(corpus)
    first = records(corpus, capsys, "sweep", "--limit", "10")
    rare = [r for r in first["records"] if r["source"].startswith("rare ")]
    assert len(rare) >= 2
    listed_cid, opened_cid = rare[0]["source"].split()[1], rare[1]["source"].split()[1]
    coverage.record("unseen", ["page=1"], listed=[listed_cid])  # seen only as a listing line
    coverage.record("expand", [opened_cid], opened=[opened_cid])  # opened by the agent
    again = [r["source"].split()[1] for r in records(corpus, capsys, "sweep", "--limit", "10")["records"]
             if r["source"].startswith("rare ")]
    assert listed_cid in again and opened_cid not in again


def test_sweep_samples_every_size_band_with_reasons_and_moves_on(corpus, capsys):
    _long_tail(corpus)
    d = records(corpus, capsys, "sweep", "--limit", "8")
    assert d["sweep"] and len(d["records"]) == 8
    sources = [r["source"] for r in d["records"]]
    assert any(s.startswith("rare ") for s in sources) and any(s.startswith("mid ") for s in sources)
    assert "posts:81" in [r["ref"] for r in d["records"]]  # the most salient rare record: listing it is not reading it
    mid = next(r for r in d["records"] if r["source"].startswith("mid "))
    assert mid["source"].endswith("×40")
    assert cli.main(["mark", *[r["ref"] for r in d["records"]], "--data", str(corpus)]) == 0
    capsys.readouterr()
    again = records(corpus, capsys, "sweep", "--limit", "8")
    assert not {r["ref"] for r in again["records"]} & {r["ref"] for r in d["records"]}


def test_crew_sweep_reads_and_reports_what_it_covered(monkeypatch):
    recs = [dict(r, source=src) for r, src in zip(RECS, ["repeated c1 ×40", "rare c3"], strict=True)]
    marked: list[str] = []
    _patch_sandbox(monkeypatch, recs, marked)
    model = FakeModel()
    out = asyncio.run(rc.sweep(rc.Crew(model=model)))
    assert "[picked as: rare c3]" in model.prompts[0]
    assert "Read: 1 repeated, 1 rare." in out and "Calling sweep again" in out
    assert marked == ["posts:1", "posts:81"]


def test_background_sweep_is_delivered_once_through_the_continue_hook(monkeypatch):
    from messageboard_audit_bench.native import combined_continue

    marked: list[str] = []
    _patch_sandbox(monkeypatch, [dict(RECS[1], source="rare c3")], marked)
    tools_turn = SimpleNamespace(output=SimpleNamespace(message=SimpleNamespace(tool_calls=["x"])))

    async def scenario():
        crew = rc.Crew(model=FakeModel(delay=0.05))
        rc.start_background_sweep(crew)
        hook = combined_continue(None, None, {}, crew=crew)
        tool = rc.crew_tool(crew)
        busy = await tool(action="sweep")
        first = await hook(tools_turn)  # still reading
        await crew.background
        second = await hook(tools_turn)
        third = await hook(tools_turn)
        return crew, busy, first, second, third

    crew, busy, first, second, third = asyncio.run(scenario())
    assert "still reading" in busy
    assert first is True and third is True
    assert second.startswith("Background reading finished") and "crew sweep: " in second
    assert crew.background_record["delivered_epoch"] >= crew.background_record["started_epoch"]
    assert crew.stats.tool_calls[0]["background"] is True


def test_tool_needs_a_target_except_for_sweep_and_option_validation():
    from messageboard_audit_bench.task import _german_wiki_report

    tool = rc.crew_tool(rc.Crew(model=object()))
    assert "give a target set" in asyncio.run(tool(action="brief"))
    assert "unknown action" in asyncio.run(tool(action="nope", target="t1"))
    s = _german_wiki_report(agent="react", tools="atlas,crew", sweep_at_start=True, time_limit_minutes=10).dataset[0]
    assert s.metadata["sweep_at_start"] is True and "crew sweep" in s.input
    with pytest.raises(ValueError):
        _german_wiki_report(agent="react", tools="atlas", sweep_at_start=True)


def test_continue_messages_name_the_arms_tools_and_baseline_is_unchanged():
    from messageboard_audit_bench.investigation_tools import continue_hint
    from messageboard_audit_bench.native import (
        _minimum_runtime_continuation,
        policy_aware_continue,
    )

    assert continue_hint(()) == ""
    hint = continue_hint(("atlas", "crew"))
    assert "widen the investigation" in hint and "crew sweep" in hint and "atlas unseen" in hint
    assert "crew" not in continue_hint(("atlas",))
    base = _minimum_runtime_continuation(elapsed_seconds=100, minimum_runtime_seconds=450, remaining_seconds=500)
    assert "widen" not in base
    with_tools = _minimum_runtime_continuation(elapsed_seconds=100, minimum_runtime_seconds=450,
                                               remaining_seconds=500, hint=hint)
    assert "crew sweep" in with_tools and with_tools.endswith(base[base.index("Keep report.md"):])
    no_tools_turn = SimpleNamespace(output=SimpleNamespace(message=SimpleNamespace(tool_calls=[])))
    msg = asyncio.run(policy_aware_continue(1000, clock=lambda: 900, hint=hint)(no_tools_turn))
    assert msg.startswith("About 100 seconds remain") and "crew sweep" in msg


def test_every_verified_note_is_kept_and_listed_by_crew_notes(monkeypatch, saved_notes):
    recs = [{"ref": f"posts:{i}", "cite": f"posts:{i} (id=r{i - 1})", "time": "-", "actor": "", "duplicates": 0,
             "text": f"id: r{i - 1}\nbody: record number {i} says something specific about step {i}"}
            for i in range(1, 31)]
    marked: list[str] = []
    _patch_sandbox(monkeypatch, recs, marked)
    crew = rc.Crew(model=FakeModel(), limits=rc.CrewLimits(chunk_records=10, notes_total=5))
    out = asyncio.run(rc.brief(crew, "t1", []))
    assert len(crew.notes) == 30 and saved_notes == crew.notes  # all verified notes kept, invented ones not
    assert sum(n["shown"] for n in crew.notes) == 1  # same-kind notes merged in the output; one shown
    assert "29 more verified notes from this call are kept: crew notes" in out
    assert all(n["key"].startswith(n["ref"] + "|") for n in crew.notes)
    again = asyncio.run(rc.brief(crew, "t1", []))  # the same notes are not stored twice
    assert len(crew.notes) == 30 and "more verified notes" not in again
    asked = []

    async def fake_list(pattern):
        asked.append(pattern)
        return "reader notes: listed by atlas"

    monkeypatch.setattr(rc, "list_notes", fake_list)  # `crew notes` delegates to `atlas notes`
    assert asyncio.run(rc.crew_tool(crew)(action="notes", target="relay")) == "reader notes: listed by atlas"
    assert asked == ["relay"]


def test_atlas_ranks_and_pushes_unseen_notes(corpus, capsys, tmp_path):
    _long_tail(corpus)
    notes = [  # posts:81 is the most salient record; posts:1 is in a 40-copy cluster
        {"kind": "times", "note": "Routine status report.", "quote": "Status report for the nightly batch job",
         "ref": "posts:1", "cite": "posts:1 (id=r0)", "shown": False},
        {"kind": "actions", "note": "Edited the hosts file to bypass the gate.", "quote": "editing /etc/hosts to point",
         "ref": "posts:81", "cite": "posts:81 (id=r80)", "shown": False},
        {"kind": "claims", "note": "A second note on the same record.", "quote": "so requests bypass the gate",
         "ref": "posts:81", "cite": "posts:81 (id=r80)", "shown": False},
        {"kind": "unexpected", "note": "Already shown by the crew.", "quote": "heartbeat ok seq 41",
         "ref": "posts:42", "cite": "posts:42", "shown": True},
    ]
    (tmp_path / "atlas-notes.jsonl").write_text("".join(json.dumps(n) + "\n" for n in notes))
    args = ["--data", str(corpus)]
    assert cli.main(["unseen", *args]) == 0
    out = capsys.readouterr().out
    section = out.split("Reader notes not shown yet")[1]
    assert section.index("posts:81") < section.index("posts:1 ")  # salient record first
    assert section.count("posts:81") == 1  # one note per record
    assert "Already shown by the crew" not in section
    assert cli.main(["unseen", *args]) == 0  # pushed notes are not pushed again
    assert "bypass the gate" not in capsys.readouterr().out.split("Rare records not opened")[1]
    assert cli.main(["notes", "hosts", *args]) == 0
    listing = capsys.readouterr().out
    assert "1 of 4 match" in listing and "posts:81" in listing
    assert cli.main(["notes", "bypass", *args]) == 0  # the full listing keeps both notes on posts:81
    assert capsys.readouterr().out.count("posts:81") == 2
    assert cli.main(["notes", "(", *args]) == 0 and "bad regex" in capsys.readouterr().out


def test_gapcheck_flags_unused_reader_notes_and_lookalikes(tmp_path, monkeypatch, capsys):
    lookalike = "admin_k\u0430rl"
    rows = [{"id": f"a{i}", "time": f"2026-01-{1 + i % 20:02d}T10:00:00Z", "user": "admin_karl",
             "body": f"routine maintenance note number {i} for the wiki"} for i in range(30)]
    rows += [{"id": "b1", "time": "2026-01-15T11:00:00Z", "user": lookalike, "body": "edit under a borrowed name"},
             {"id": "c1", "time": "2026-01-16T09:00:00Z", "user": "bot7",
              "body": "moved the relay to tunnel-host-77.example.link before midnight"},
             {"id": "c2", "time": "2026-01-16T09:30:00Z", "user": "bot7", "body": "ordinary status update for today"}]
    d = tmp_path / "data"
    d.mkdir()
    with (d / "posts.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    monkeypatch.setenv("ATLAS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("ATLAS_STATE", str(tmp_path / "state.jsonl"))
    notes = [{"kind": "unexpected", "note": "The relay moved to a tunnel host.", "ref": "posts:32",
              "quote": "moved the relay to tunnel-host-77.example.link", "cite": "posts:32 (id=c1)", "source": "sweep"},
             {"kind": "times", "note": "A status update.", "ref": "posts:33", "quote": "ordinary status update for today",
              "cite": "posts:33 (id=c2)", "source": "sweep"}]
    (tmp_path / "atlas-notes.jsonl").write_text("".join(json.dumps(n) + "\n" for n in notes))
    report = tmp_path / "report.md"
    report.write_text("# Report\n\nThe admin admin_karl did routine maintenance (posts:1). A status update was "
                      "posted on 2026-01-16 [posts:33].\n")
    assert cli.main(["gapcheck", str(report), "--data", str(d)]) == 0
    out = capsys.readouterr().out
    assert "a reader noted (unexpected, posts:32 (id=c1))" in out and "tunnel-host-77" in out
    assert "posts:33" not in out.split("Consider")[1]  # cited in the report: not flagged
    assert f"look-alike identifier: '{lookalike}'" in out
    report.write_text(report.read_text() + f"\nAn impersonator used {lookalike} [posts:31]; the relay moved to "
                      "tunnel-host-77.example.link [posts:32].\n")
    assert cli.main(["gapcheck", str(report), "--data", str(d)]) == 0
    out = capsys.readouterr().out
    assert "a reader noted" not in out and "look-alike identifier" not in out
