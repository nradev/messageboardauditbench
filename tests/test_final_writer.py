"""The final writer: inputs, the repair loop, fallbacks to the draft, and task options."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from messageboard_audit_bench import final_writer as fw

DRAFT = "# Report\n\nTL;DR: something happened (posts:1).\n"
PACK = {"level": "W2", "fix": 1, "consider": 2, "gapcheck": "1 to fix, 2 to consider\nFix ...",
        "notes": ["(actions) Edited the hosts file — \"editing /etc/hosts\" [posts:81]"], "cited": [], "opened": [],
        "map": ""}


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[list] = []
        self.configs: list = []

    async def generate(self, messages, config=None):
        self.calls.append(list(messages))
        self.configs.append(config)
        text = self.replies.pop(0)
        if isinstance(text, Exception):
            raise text
        return SimpleNamespace(completion=text, message=SimpleNamespace(role="assistant", content=text),
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=300))


def _setup(monkeypatch, replies, checks):
    model = FakeModel(replies)
    written: dict[str, str] = {}
    checks = list(checks)

    async def write(path, text):
        written[path] = text

    async def atlas_json(argv):
        return dict(PACK) if argv[1] == "pack" else checks.pop(0)

    async def install():
        written["installed"] = "yes"

    monkeypatch.setattr(fw, "_write", write)
    monkeypatch.setattr(fw, "_atlas_json", atlas_json)
    monkeypatch.setattr(fw, "install_atlas", install)
    monkeypatch.setattr(fw, "get_model", lambda role=None, default=None: model)
    return model, written


def _run(**kw):
    args = dict(min_words=0, max_words=50, tokens_left=lambda: 20000, deadline=time.monotonic() + 60, install=False)
    args.update(kw)
    return asyncio.run(fw.run_writer("W2", "Write report.md with a TL;DR.", DRAFT, **args))


CLEAN = {"fix": 0, "new_fix": [], "refs_outside": []}


def test_inputs_hold_the_draft_gapcheck_notes_and_limits():
    text = fw.build_inputs("Task text.", DRAFT, PACK, 100, 3000)
    for part in ("Task text.", DRAFT.strip(), "1 to fix", "editing /etc/hosts", "at most 3,000 words",
                 "at least 100"):
        assert part in text
    assert "Corpus map" not in text and "Excerpts" not in text  # absent sections are left out
    w3 = fw.build_inputs("T", DRAFT, {**PACK, "cited": [{"ref": "posts:1", "text": "body: x"}], "map": "themes"},
                         0, 0)
    assert "[posts:1]\nbody: x" in w3 and "Corpus map (context only: never cite it)" in w3


def test_extract_report():
    assert fw.extract_report("noise <report>\n# R\nbody\n</report> more") == "# R\nbody"
    assert fw.extract_report("```markdown\n# R\nbody\n```") == "# R\nbody"
    assert fw.extract_report("I could not do it.") is None
    assert fw.extract_report("<report> </report>") is None


def test_clean_rewrite_replaces_the_draft(monkeypatch):
    model, written = _setup(monkeypatch, ["<report># Final\n\nBetter (posts:1).</report>"], [CLEAN])
    res = _run()
    assert res.report == "# Final\n\nBetter (posts:1)." and res.meta["status"] == "replaced"
    assert written[fw.DRAFT_PATH] == DRAFT and "editing /etc/hosts" in written[fw.INPUTS_PATH]
    assert res.meta["calls"] == 1 and res.meta["output_tokens"] == 300 and res.meta["notes_given"] == 1
    assert res.meta["notes"] == PACK["notes"]  # what the writer was offered, for auditing
    assert model.configs[0].max_tokens == 20000
    assert "installed" not in written


def test_one_repair_then_accept(monkeypatch):
    bad = {"fix": 1, "new_fix": ['"x y z" is not in posts:2'], "refs_outside": ["posts:9"]}
    model, _ = _setup(monkeypatch, ["<report># A (posts:9)</report>", "<report># B (posts:1)</report>"], [bad, CLEAN])
    res = _run()
    assert res.report == "# B (posts:1)" and res.meta["calls"] == 2
    repair = model.calls[1][-1].content
    assert "is not in posts:2" in repair and "posts:9" in repair
    assert len(res.meta["first_issues"]) == 2


def test_problems_that_remain_keep_the_draft(monkeypatch):
    long = "<report># R\n\n" + "word " * 80 + "</report>"
    _setup(monkeypatch, [long, long], [CLEAN, CLEAN])
    res = _run()
    assert res.report is None and res.meta["status"] == "kept_draft"
    assert "above the limit of 50" in res.meta["issues"][0]


@pytest.mark.parametrize("replies,status", [
    (["Sorry, no report."], "kept_draft"),
    ([RuntimeError("provider down")], "error"),
])
def test_failures_keep_the_draft(monkeypatch, replies, status):
    _setup(monkeypatch, replies, [CLEAN])
    res = _run()
    assert res.report is None and res.meta["status"] == status


def test_no_time_left_and_empty_draft(monkeypatch):
    _setup(monkeypatch, ["<report># R</report>"], [CLEAN])
    assert _run(deadline=time.monotonic() + 1).meta["status"] == "error"
    res = asyncio.run(fw.run_writer("W1", "T", "  ", min_words=0, max_words=0, tokens_left=None,
                                    deadline=time.monotonic() + 60, install=False))
    assert res.meta["status"] == "skipped"


def test_installs_atlas_for_arms_without_it_and_floors_max_tokens(monkeypatch):
    model, written = _setup(monkeypatch, ["<report># R</report>"], [CLEAN])
    _run(install=True, tokens_left=lambda: 10)
    assert written["installed"] == "yes" and model.configs[0].max_tokens == fw.MIN_TOKENS


def test_task_options_split_the_budget():
    from messageboard_audit_bench.task import _german_wiki_report as g

    s = g(agent="react", config="blind-tokens", token_budget=200000, writer="W1").dataset[0]
    assert s.metadata["agent_budget"] == {"tokens": 180000} and "180,000 output tokens" in s.input
    assert s.id.endswith("+writer-W1")
    s = g(agent="react", time_limit_minutes=30, tools="atlas", writer="W3", writer_reserve=0.2).dataset[0]
    assert s.metadata["agent_budget"] == {"minutes": 24} and "24 minutes" in s.input
    plain = g(agent="react", config="blind-tokens", token_budget=200000).dataset[0]
    assert "writer" not in plain.metadata and "200,000 output tokens" in plain.input
    for bad in (dict(writer="W4"), dict(writer="W1", writer_reserve=0.6), dict(writer="W1", agent="claude")):
        with pytest.raises(ValueError):
            g(**{"agent": "react", "time_limit_minutes": 30, **bad})


@pytest.mark.asyncio
async def test_solver_runs_the_writer_after_the_agent_and_keeps_the_draft(monkeypatch):
    from inspect_ai.agent import AgentState
    from inspect_ai.model import ChatMessageAssistant

    import messageboard_audit_bench.native as native
    from tests.test_solver import _state

    files = {native.REPORT_PATH: DRAFT}
    seen = {}

    class FakeSandbox:
        async def exec(self, cmd, input=None, timeout=None):
            if cmd[:2] == ["sh", "-c"] and cmd[2] == f"cat > {native.REPORT_PATH}":
                files[native.REPORT_PATH] = input
                return SimpleNamespace(success=True, stdout="", stderr="")
            return SimpleNamespace(success=False, stdout="", stderr="")

        async def read_file(self, path):
            return files[path]

    async def fake_report():
        return files[native.REPORT_PATH], None

    async def fake_run(*_a, **_k):
        return AgentState(messages=[*_state().messages, ChatMessageAssistant(content="done")]), None

    async def fake_writer(level, task_prompt, draft, **kw):
        seen.update(level=level, draft=draft, **kw)
        return fw.WriterResult("# Final\n\nRewritten (posts:1).", {"status": "replaced"})

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(native, "inspect_agent", lambda *_a, **_k: object())
    monkeypatch.setattr(native, "_prepare_budget", nothing)
    monkeypatch.setattr(native, "run", fake_run)
    monkeypatch.setattr(native, "_read_report", fake_report)
    monkeypatch.setattr(native, "sandbox", lambda: FakeSandbox())
    monkeypatch.setattr(native, "run_writer", fake_writer)

    solver = native.inspect_native_agent("react", 600, min_runtime_fraction=0, writer="W1", writer_seconds=60)
    state = await solver(_state(), None)
    assert seen["level"] == "W1" and seen["draft"] == DRAFT and seen["install"] is True
    assert seen["tokens_left"] is None  # a time budget: no token allowance
    assert state.output.completion.startswith("# Final")
    assert state.metadata["writer_draft_report"] == DRAFT
    assert state.metadata["writer"]["status"] == "replaced" and state.metadata["writer"]["reserve_seconds"] == 60
