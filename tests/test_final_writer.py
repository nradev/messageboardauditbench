"""The final writer: inputs, the repair loop, fallbacks to the draft, and task options."""

from __future__ import annotations

import asyncio
import re
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
                 "at least 100", "aim for about 2,700"):
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
    assert model.configs[0].max_tokens == 12000  # 60% of what is left: room for a repair call
    assert "installed" not in written


def test_one_repair_then_accept(monkeypatch):
    bad = {"fix": 1, "new_fix": ['"x y z" is not in posts:2'], "refs_outside": ["posts:9"]}
    model, written = _setup(monkeypatch, ["<report># A (posts:9)</report>", "<report># B (posts:1)</report>"],
                            [bad, CLEAN])
    written_inputs = []
    orig_write = fw._write

    async def track(path, text):
        if path == fw.INPUTS_PATH:
            written_inputs.append(text)
        await orig_write(path, text)

    monkeypatch.setattr(fw, "_write", track)
    res = _run()
    assert res.report == "# B (posts:1)" and res.meta["calls"] == 2
    repair = model.calls[1][-1].content
    assert "is not in posts:2" in repair and "posts:9" in repair
    assert written_inputs[-1].endswith(repair.strip())  # refs named in the repair count as inputs
    assert len(res.meta["first_issues"]) == 2


LONG = ("<report># R\n\n## TL;DR\n\nThe summary stays.\n\n## Findings\n\n" + "keep " * 20 + "\n\n- "
        + "minor " * 30 + "\n- " + "also " * 10 + "\n</report>")


def test_units_fix_headings_and_the_summary():
    us = fw.units(fw.extract_report(LONG))
    free = [k for k, u in enumerate(us) if not u["fixed"]]
    texts = ["\n".join(us[k]["lines"]).strip() for k in free]
    assert len(free) == 3 and texts[1].startswith("- minor") and texts[2].startswith("- also")
    assert all("summary stays" not in t for t in texts)
    report, removed = fw.apply_trim(us, f"0, {free[1]}", target=40)  # a fixed unit is never deleted
    assert removed == 1 and "minor" not in report and report.startswith("# R") and "summary stays" in report
    # In the reply's order, stopping at the target, never below the floor.
    report, removed = fw.apply_trim(us, f"{free[2]}, {free[1]}, {free[0]}", target=50)
    assert removed == 2 and "keep" in report and fw.count_words(report) <= 50
    report, removed = fw.apply_trim(us, f"{free[1]}, {free[0]}", target=10, floor=35)
    assert removed == 1 and "keep" in report and "minor" not in report
    assert "[" + str(free[1]) + "] (31 words)" in fw.trim_prompt(fw.extract_report(LONG), us, 20)


def test_overlong_report_is_trimmed_by_deleting_chosen_units(monkeypatch):
    def pick(prompt_text):
        return re.search(r"\[(\d+)\] \(31 words\)", prompt_text).group(1)

    model, _ = _setup(monkeypatch, [LONG], [CLEAN, CLEAN])
    orig = model.generate

    async def gen(messages, config=None):
        if messages[0].content.startswith("This report has"):
            model.calls.append(list(messages))
            text = pick(messages[0].content)
            return SimpleNamespace(completion=text, message=None, usage=SimpleNamespace(input_tokens=10, output_tokens=5))
        return await orig(messages, config)

    model.generate = gen
    res = _run()
    assert res.meta["status"] == "replaced" and res.meta["trims"] == 1 and res.meta["units_deleted"] == 1
    assert "minor" not in res.report and "keep" in res.report and res.meta["final_words"] <= 50
    assert "above the limit of 50" in res.meta["first_issues"][0]


def test_trimming_that_does_not_reach_the_limit_keeps_the_draft(monkeypatch):
    model, _ = _setup(monkeypatch, [LONG, "nothing", "nothing"], [CLEAN])
    res = _run()
    assert res.report is None and res.meta["trims"] == 2 and res.meta["reason"] == "outside the word limits"


def test_other_problems_get_only_one_repair(monkeypatch):
    bad = {"fix": 1, "new_fix": ["x"], "refs_outside": []}
    model, _ = _setup(monkeypatch, ["<report># A</report>", "<report># B</report>"], [bad, bad])
    res = _run()
    assert res.meta["status"] == "kept_draft" and res.meta["repairs"] == 1 and len(model.calls) == 2


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
    s = g(agent="react", time_limit_minutes=30, tools="atlas", writer="W3", writer_reserve=0.2,
          writer_strength="rewrite").dataset[0]
    assert s.id.endswith("+writer-W3-rewrite") and s.metadata["writer_strength"] == "rewrite"
    assert s.metadata["agent_budget"] == {"minutes": 24} and "24 minutes" in s.input
    plain = g(agent="react", config="blind-tokens", token_budget=200000).dataset[0]
    assert "writer" not in plain.metadata and "200,000 output tokens" in plain.input
    for bad in (dict(writer="W4"), dict(writer="W1", writer_reserve=0.6), dict(writer="W1", agent="claude"),
                dict(writer="W1", writer_strength="heavy")):
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


def test_strength_sets_the_approach_rule_only(monkeypatch):
    prompts = {k: fw.system_prompt(k) for k in fw.STRENGTHS}
    assert "Keep every supported finding" in prompts["edit"]
    assert "make room" in prompts["rebalance"] and "Write the report anew" in prompts["rewrite"]
    shared = prompts["edit"].split(fw.APPROACH["edit"])[1]
    assert all(p.endswith(shared) for p in prompts.values())  # the other rules are the same
    model, _ = _setup(monkeypatch, ["<report># R</report>"], [CLEAN])
    res = _run(strength="rewrite")
    assert res.meta["strength"] == "rewrite" and "Write the report anew" in model.calls[0][0].content


def test_repair_call_gets_the_rest_of_the_allowance(monkeypatch):
    left = [20000]
    bad = {"fix": 1, "new_fix": ["x"], "refs_outside": []}
    model, _ = _setup(monkeypatch, ["<report># A</report>", "<report># B</report>"], [bad, CLEAN])
    orig = model.generate

    async def spend(messages, config=None):
        out = await orig(messages, config)
        left[0] -= 11000
        return out

    model.generate = spend
    _run(tokens_left=lambda: left[0])
    assert [c.max_tokens for c in model.configs] == [12000, 9000]


def test_parse_variants():
    assert fw.parse_variants("W3:edit, W3:rewrite,W1") == (("W3", "edit"), ("W3", "rewrite"), ("W1", "edit"))
    assert fw.parse_variants(["W2:rebalance"]) == (("W2", "rebalance"),)  # Inspect's CLI passes a list
    assert fw.parse_variants(None) == ()
    for bad in ("W4:edit", "W3:heavy", "W3,W3:edit"):
        with pytest.raises(ValueError):
            fw.parse_variants(bad)


@pytest.mark.asyncio
async def test_variant_scorer_grades_the_variant_report_and_leaves_the_state_alone():
    from inspect_ai.model import ModelOutput
    from inspect_ai.scorer import Score

    seen = []

    async def inner(state, target):
        seen.append(state.output.completion)
        return Score(value=len(state.output.completion))

    state = SimpleNamespace(metadata={"writer_variants": {"W3-edit": {"report": "# Final", "replaced": True}}},
                            output=ModelOutput.from_content(model="m", content="# Draft"))
    sc = fw.variant_scorer(inner, "W3-edit", "v2_W3_edit_test")
    res = await sc(state, None)
    assert seen == ["# Final"] and res.value == 7 and res.metadata["writer_variant"] == "W3-edit"
    assert state.output.completion == "# Draft"  # the sample's own output is untouched
    missing = await fw.variant_scorer(inner, "W1-edit", "v2_W1_edit_test")(state, None)
    assert missing.answer == "ungraded"


def test_task_variants_keep_the_full_budget_and_add_scorers():
    from inspect_ai._util.registry import registry_info

    from messageboard_audit_bench.task import _german_wiki_report as g

    t = g(agent="react", config="blind-tokens", token_budget=200000, tools="atlas",
          writer_variants="W3:edit,W3:rewrite")
    s = t.dataset[0]
    assert "200,000 output tokens" in s.input and s.metadata["writer_variants"] == ["W3-edit", "W3-rewrite"]
    names = [registry_info(x).name.split("/")[-1] for x in t.scorer]
    assert names[:2] == ["sheet_scorer", "sheet_scorer"]  # the agent's own report, as before
    assert {"v2_W3_edit", "tldrh_W3_edit", "v2_W3_rewrite", "tldrh_W3_rewrite"} <= set(names)
    base = g(agent="react", config="blind-tokens", token_budget=200000, tools="atlas")
    assert t.time_limit == base.time_limit + 2 * 10 * 60 + 3 * 60  # 2 variants x 10 min + setup
    timed = g(agent="react", time_limit_minutes=30, writer_variants="W1").dataset[0]
    assert "30 minutes" in timed.input
    with pytest.raises(ValueError):
        g(agent="react", time_limit_minutes=30, writer="W1", writer_variants="W3:edit")


@pytest.mark.asyncio
async def test_solver_runs_each_variant_outside_the_budget_and_keeps_the_report(monkeypatch):
    from inspect_ai.agent import AgentState
    from inspect_ai.model import ChatMessageAssistant

    import messageboard_audit_bench.native as native
    from tests.test_solver import _state

    files = {native.REPORT_PATH: DRAFT}
    calls = []
    usage = [1000]

    async def fake_report():
        return files[native.REPORT_PATH], None

    async def fake_run(*_a, **_k):
        return AgentState(messages=[*_state().messages, ChatMessageAssistant(content="done")]), None

    async def fake_writer(level, task_prompt, draft, **kw):
        calls.append((level, kw["strength"], kw["tokens_left"](), id(kw["cache"])))
        usage[0] += 5000  # the writer's tokens
        if kw["strength"] == "rewrite":
            return fw.WriterResult(None, {"status": "kept_draft"})
        return fw.WriterResult(f"# {level} {kw['strength']}", {"status": "replaced"})

    async def nothing(*_a, **_k):
        return None

    class FakeSandbox:
        async def exec(self, *a, **k):
            return SimpleNamespace(success=False, stdout="", stderr="")

    monkeypatch.setattr(native, "inspect_agent", lambda *_a, **_k: object())
    monkeypatch.setattr(native, "_prepare_budget", nothing)
    monkeypatch.setattr(native, "run", fake_run)
    monkeypatch.setattr(native, "_read_report", fake_report)
    monkeypatch.setattr(native, "sandbox", lambda: FakeSandbox())
    monkeypatch.setattr(native, "run_writer", fake_writer)
    monkeypatch.setattr(native, "sample_output_tokens", lambda: usage[0])
    monkeypatch.setattr("messageboard_audit_bench.token_budget.sample_output_tokens", lambda: usage[0])

    solver = native.inspect_native_agent("react", 600, min_runtime_fraction=0, output_token_budget=2000,
                                         writer_variants=(("W3", "edit"), ("W3", "rewrite")),
                                         writer_tokens=20000, writer_seconds=600)
    state = await solver(_state(), None)
    assert [c[:3] for c in calls] == [("W3", "edit", 20000), ("W3", "rewrite", 20000)]  # each its own allowance
    assert calls[0][3] == calls[1][3]  # one shared cache: atlas install and inputs prepared once
    v = state.metadata["writer_variants"]
    assert v["W3-edit"]["report"] == "# W3 edit" and v["W3-edit"]["replaced"] is True
    assert v["W3-rewrite"]["report"] == DRAFT and v["W3-rewrite"]["replaced"] is False
    assert state.output.completion == DRAFT and files[native.REPORT_PATH] == DRAFT  # the agent's report
    assert state.metadata["budget_tokens_used"] == 0  # the agent's usage, without the 10k of the writers
