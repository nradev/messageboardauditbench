# Benchmark versions

The repository holds two benchmarks, each a separate Inspect eval with its own version.
Each version is a git tag, and one flag runs any tagged version. The published LessWrong
results were run at German wiki report version `6.1` (labelled `6-B` at the time).

| benchmark | Inspect task | current version | storage id |
|---|---|---:|---|
| German wiki report | `german_wiki_report` | `12.1` | `messageboard` |
| Transluce report | `transluce_report` | `1.0` | `urlquery` |

The storage id is what run records, configs and grade files write as `benchmark_id`. It
predates the public names and does not change. The tasks were called
`messageboard_audit_bench` and `urlquery_audit_bench` before 2026-09-29. Those names, and
the `_replay`, `_continue`, `grade_reports` and `urlquery_grade_reports` variants, still
work as aliases.

## What a version means

A version is `MAJOR.MINOR` and names the behaviour of the task code: what the agent
sees, what it can do, whether its report is accepted, and how the default grading scores
it.

- **MAJOR** goes up when a change can make results incomparable with earlier runs.
- **MINOR** goes up for a change that keeps results comparable, such as a renamed option
  or a new optional feature.

Code-only refactors that preserve behaviour need no bump. The version is declared once
per benchmark, in `SPECS` in `messageboard_audit_bench/benchmarks.py`. Every Inspect log
records it as the task version.

The version does not pin everything a score depends on. The config (budget, prompt, data
variant, effort), the model, the judge and the rubric are separate conditions. A score is
comparable to a published cell only when the version and those conditions all match.

## Running a specific version

Use `scripts/run_eval.py`. Everything after `--` goes to `inspect eval`:

```bash
# this checkout's version (the task's own version guard confirms it)
uv run python scripts/run_eval.py german-wiki-report -- -T agent=codex --model openai/gpt-5.6-sol

# an earlier version, from its tag
uv run python scripts/run_eval.py german-wiki-report --version 9.0 -- -T agent=codex --model openai/gpt-5.6-sol

# grade, replay or continue at a version
uv run python scripts/run_eval.py transluce-report --version 1.0 --task grade -- -T launch=runs/urlquery/<final>/launch.json

uv run python scripts/run_eval.py german-wiki-report --list        # tagged versions
uv run python scripts/run_eval.py german-wiki-report --version 8.0 --dry-run -- ...
```

**This version.** The eval runs in this checkout, with `-T version=<MAJOR.MINOR>` added.

**Any other version.** The launcher checks the tag `<benchmark>-v<version>` out, detached,
into `.worktrees/version-<tag>/`. It gives the checkout its own `.venv` and links in the
primary checkout's `data/`, `runs/`, `logs/` and `.env`, so the old code reads the same
inputs and writes to the same archive. The checkout is created once and reused.
Versions before `10.0` / `1.0` predate the task rename and the version guard; the
launcher uses the task name that version had.

**The version guard.** You can pass `-T version=` directly to `german_wiki_report` or
`transluce_report`. The task refuses to start if the checkout is a different version, and
the error names the command to run instead. `10`, `10.0`, `v10.0` and the old label
`10-A` all mean `10.0`.

## German wiki report

The [LessWrong post, "How good are slop-vestigators?"](https://www.lesswrong.com/posts/wt4kk6vFPEhkXvF8Q/how-good-are-slop-vestigators)
reports the collusion.wiki round 4 results and the longer-report and provider-swap
followups. Their archived generation logs record Inspect task version `6-B`, now `6.1`.
That is the run version, even though the repository moved to later versions while the
results were being graded and packaged.

The old labels were `<major>-<letter>`, and they convert one-to-one: letter A is minor 0,
B is minor 1. Each tag points at the last commit that carried its version.

| version | old label | tag (commit) | What it identifies |
|---|---|---|---|
| `6.1` | `6-B` | `german-wiki-report-v6.1` (`45350ea`) | The September 7–8 round 4, followup and provider-swap generation runs used in the LessWrong analysis. |
| `7.0` | `7-A` | `german-wiki-report-v7.0` (`438392c`) | The September 8 code after inline benchmark grading was added. The older `inspect-logs-2026-09-08` tag also points into this version; the archived runs themselves still say `6-B`. |
| `8.0` | `8-A` | `german-wiki-report-v8.0` (`d347133`) | Introduced with the September 11 Mythos 5 incident and kept through later changes, including RubyHack and the September 26 report-count changes. It did not define the LessWrong runs. |
| `9.0` | `9-A` | `german-wiki-report-v9.0` (`e52e499`) | The September 26 report feedback and word-count rule. |
| `10.0` | `10-A` | `german-wiki-report-v10.0` | The shared runtime changes merged with the Transluce report (below). Also the task rename and version flag, which do not change behaviour. |
| `11.0` | | not yet tagged | The default judge is Claude Opus 5.5, and the `blind-tokens` output-token budget is added (below). |
| `12.0` | | not yet tagged | Native ReAct tools use the standard schema except for OpenAI models routed through OpenRouter (below). |
| `12.1` | | not yet tagged | Faster grading: concurrent sheets and an optional judge effort (below). |

The archived logs provide the direct evidence for the LessWrong run version: the
`task_version` field is `6-B` in the local round 4, followup, and provider-swap cohorts.
Their `revision.commit` fields vary across runs, so no single source commit covers every
generation run. The `v6.1` tag is the last `6-B` commit, a way to rerun that behaviour. It
should not be substituted for the revision in each log.

The [Inspect log archive](artifacts/inspect-logs.md) and the
[publication evidence index](benchmark-data-index.md) map selected reports to logs and
to the separately retained Fable 5.1 `v2` and `tldrh` grades. The published headline
combines those grades at 70% finding coverage and 30% holistic TLDR assessment.

`8.0` was kept through behaviour changes, and its tag holds only the last of them. To
tell apart runs made during that interval, use each log's Git revision and prompt
provenance.

### Why each bump

**`9.0`.** Agents now receive report and TLDR word counts after every report edit, and
the prompt describes that feedback. Complete inline Markdown links no longer count
towards the report word count. These changes alter both what the agent is told and the
acceptance calculation. The revised count method is recorded as
`whitespace-no-inline-links-v2` in report metadata.

**`10.0`.** The runtime policy shared with the Transluce report changed in two ways:

- **Stop rule.** The runtime policy decides when an agent that stops early is sent back
  to work. It now treats only two things as a reason to stop: an explicit refusal of the
  task, or a terminal status such as `error` or `refused`. Before, any mention of
  "error", "fail" or "I can't" in a status field or the final message counted.
- **ReAct errors.** The ReAct scaffold now stops on a provider error returned inside an
  HTTP 200 response, instead of continuing.

Prompts, data, rubrics and the judge are unchanged. Two further changes in `10.0` do not
alter behaviour:

- The task was renamed from `messageboard_audit_bench`.
- The Mythos 5 and RubyHack drafts are no longer selectable as configs; they are drafts
  for separate evals.

**`11.0`.** The default judge changed from `openai/gpt-5.6-sol` to
`anthropic/claude-opus-5-5` on the audit, replay, continuation and grading tasks and in
`scripts/run_inspect_matrix.sh`, so default scores are not comparable with `10.0`. An
explicit `-T judge=` or `--model-role grader=` still selects any judge; the published
comparison set remains Fable 5.1 grades. Prompts, data, rubrics and the existing configs
are unchanged. Also new, and not affecting existing configs:

- The `blind-tokens` config (prompt `blind-v2-tokens`) gives native ReAct an output-token
  budget instead of a time budget: the turn that crosses it finishes, then one final turn
  to finish report.md. `-T token_budget=N` sets it; `time_limit_minutes` becomes a
  wall-clock backstop.
- Native runs record the provider-billed cost (`cost_source: provider`) when Inspect has
  no price for the model.

**`12.0`.** Native ReAct used to declare every `bash` and `text_editor` parameter as
required, so that OpenRouter could route OpenAI models to Azure. Models that call
`text_editor` without the unused, nullable arguments then had the call rejected:
GPT-6 Luna (direct OpenAI) and MiMo v2.6 Flash lost their reports this way at `11.0`.
Only `openrouter/openai/*` models now get the all-required schema; every other model
sees Inspect's standard one (`command` and `path` required). Each ReAct sample records
`react_tool_schema` (`all_required` or `standard`). ReAct results at `12.0` are not
comparable with earlier versions for models that omit nullable arguments; CLI scaffolds
are unaffected.

**`12.1`.** The sheet scorer grades a report's sheets concurrently instead of one after
another, combining them in sheet order, so default grades are unchanged. A new
`judge_effort` option (on the audit and grading tasks, `--judge-effort` on
`run_inspect_matrix.sh`, and `scripts/grade_staged.sh`, which runs the findings and TL;DR
sheets together) sets the judge's starting effort; the default stays `xhigh`. On three
DeepSeek V4.1 Flash reports, GPT-6.1 Sol at `medium` matched its `xhigh` grades to within
0.011 combined (88% of finding scores within 0.1) at a fifth of the cost, and the batch
took 31 s instead of about 10 minutes. Compare only grades made at the same effort.

For a short while on 2026-09-28 the code said `10-A`, with the old task name and
identical behaviour.

## Transluce report

The conditions are in `benchmarks/urlquery/benchmark.json`: dataset snapshot and hash,
configs, rubric, headline weights and default judge.

| version | old label | tag | What it identifies |
|---|---|---|---|
| `1.0` | `1-A` | `transluce-report-v1.0` | The first Inspect version: snapshot `2026-09-26-v1`, prompt `urlquery-agents-v6`, reviewed rubric F1–F13 with F3 weighted 0.5. |

**The final run.** The final 2026-09-27/28 generation runs used the same prompt, configs
and dataset. They went through the batch launcher (`urlquery_pilot`) before the task
existed, so their run records carry no version. They also say `rubric_version: null` and
`scoring_status: "unscored_pending_manual_rubric"`.

**New run records.** Runs from this version record `benchmark_version: "1.0"` (`"1-A"`
for the few made before the rename), `rubric_version: "reviewed"` and
`scoring_status: "ungraded"`. Filter on the version, not on those labels.

## Releasing a new version

1. **Bump.** Change the version in `SPECS` before collecting results with the new code.
2. **Record why.** Add a row and a "why" paragraph here.
3. **Tag.** Tag the commit that ships it as `<benchmark>-v<version>`, then push the tag
   (`git push origin <tag>`).
4. **Keep the old runs.** Keep earlier logs and grades under their original version.

For a reportable result, keep the `.eval` log or equivalent provenance: benchmark
version, Git revision and dirty status, rendered prompt, corpus digests, budget, model,
scaffold, judge and rubric.

A new incident or rubric can share task code, but it becomes its own eval, with its own
name and version, rather than a new config of an existing one.
