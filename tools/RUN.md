# Running the investigation-tool experiments

Commands for building the data, trying `atlas` locally, and running the paired
pilots (with and without `atlas`) on the German wiki report. Run everything from
the repository root. Design: `TOOL_IDEAS.md`; history and findings:
`IMPLEMENTATION_LOG.md`.

## 1. Setup

`.env` in the repository root (gitignored; Inspect reads it):

```sh
OPENROUTER_API_KEY=...   # agent models (openrouter/...)
OPENAI_API_KEY=...       # judge (openai/gpt-6.1-sol)
```

Build the data. If `collusion.wiki` is unreachable, use the Wayback copy; the
build checks it against the pinned SHA256, so any copy that passes is the
benchmark dataset.

```sh
scripts/build_data.sh
# or, if the upstream host times out:
curl -sSL -o /tmp/full-wiki-logs.zip \
  "https://web.archive.org/web/20260904112141id_/https://collusion.wiki/explorer/download/full-wiki-logs.zip"
MBAB_DUMP_ARCHIVE=/tmp/full-wiki-logs.zip scripts/build_data.sh
```

## 2. atlas locally (no cost)

```sh
tools/atlas/bin/atlas --data data/verbatim overview
tools/atlas/bin/atlas --data data/verbatim themes
tools/atlas/bin/atlas --data data/verbatim expand t1
tools/atlas/bin/atlas --data data/verbatim entities
tools/atlas/bin/atlas --data data/verbatim grep 'timeout|retry'
```

Other corpora: `--data data/mythos5`, `--data data/rubyhack`. Set
`ATLAS_STATE=/tmp/atlas-coverage.jsonl` to keep the coverage log out of your
home directory.

Clustering diagnostics (generic; thresholds were frozen on these):

```sh
python3 tools/atlas/validate_clusters.py data/verbatim
```

Tests and lint:

```sh
uv run ruff check . && uv run pytest -q tests/test_atlas.py
```

## 3. Paired pilot: GLM 5.3, 10 minutes, 3 epochs per condition

The two conditions differ only by `-T tools=atlas` (which adds the atlas tool
and one prompt paragraph). Run them in parallel.

Flags shared by both:

- `--time-limit 2400`: Inspect gives scoring half the sample time limit. The
  default (budget + 5 min guard) leaves the 8 sequential `v2` sheets too
  little time; 40 min gives scoring 20. The agent's own budget is unchanged.
- `-T policy_aware_continue=true` (both conditions): agents that finish their
  report early kept calling `submit`, prompted by Inspect's default "if you
  believe you have completed the task, call submit()" nudge, until the
  minimum-runtime rule (8 refused early completions) failed the sample. With
  this option, before the earliest acceptable finish the nudge says how long
  remains and to keep investigating, without mentioning submit; and reaching
  the cap accepts the report with `minimum_runtime_violation: true` in sample
  metadata instead of an error. Off by default (the published condition), so
  results are comparable to each other but not exactly to round 4.
- `-M provider=…`: pins OpenRouter to the `wafer` provider for GLM 5.3,
  with no fallback. Provider switching made many calls miss the prompt cache
  (each miss re-sends the whole context); pinning keeps the cache warm and
  makes both conditions run on the same backend. If that provider is down,
  calls fail instead of silently moving elsewhere.
- `--no-fail-on-error`: one failed sample does not cancel the other epochs.
  The common cause of a sample error is the minimum-runtime rule: an agent
  that calls `submit` more than 8 times before 75% of its budget is failed.
  No retries (`--retry-on-error`), to keep costs predictable.
- `--score-on-error`: a sample that errors is graded
  anyway (its report is saved before the error). **Such a run broke the
  benchmark's minimum-runtime rule; check `usage_metrics.py` output for
  `error:` lines and report those runs separately.**

With atlas:

```sh
uv run inspect eval messageboard_audit_bench/german_wiki_report \
  -T agent=react -T tools=atlas -T time_limit_minutes=10 -T policy_aware_continue=true \
  --model openrouter/z-ai/glm-5.3 --model-role grader=openai/gpt-6.1-sol \
  -M provider='{"order": ["wafer"], "allow_fallbacks": false}' \
  --time-limit 2400 --epochs 3 --max-samples 3 \
  --no-fail-on-error --score-on-error \
  --log-dir logs/atlas-pilot3
```

Baseline (published condition):

```sh
uv run inspect eval messageboard_audit_bench/german_wiki_report \
  -T agent=react -T time_limit_minutes=10 -T policy_aware_continue=true \
  --model openrouter/z-ai/glm-5.3 --model-role grader=openai/gpt-6.1-sol \
  -M provider='{"order": ["wafer"], "allow_fallbacks": false}' \
  --time-limit 2400 --epochs 3 --max-samples 3 \
  --no-fail-on-error --score-on-error \
  --log-dir logs/atlas-baseline3
```

Grade inline as above. Deferring with `--no-score` is possible, but
`inspect score` cannot currently rebuild these scorers from a log
(`LookupError: sheet_scorer was not found in the registry`), so grading a
deferred log needs a small script that runs the task's scorers on it.

## 4. Read the results

Per sample: headline score (coverage = mean `max(2s-1, 0)` over the 38
findings; combined = 0.7 × coverage + 0.3 × TL;DR), atlas usage (calls by
subcommand, share, first/last use, `unseen` calls), clusters opened, coverage of
the 50 most salient small clusters, report words, errors.

```sh
uv run python tools/atlas/usage_metrics.py logs/atlas-pilot3/*.eval --data data/verbatim
uv run python tools/atlas/usage_metrics.py logs/atlas-baseline3/*.eval
uv run inspect view --log-dir logs/atlas-pilot3
```

Round-4 reference for this model and budget (Fable 5.1 judge, not directly
comparable with gpt-6.1-sol): GLM 5.3 react, 10 min, combined 0.34 (0.31–0.39
over 3 runs).

## 5. No-cost end-to-end check

After changing atlas or the integration, check the full path in the real
sandbox with a scripted mock model (no API calls): run the task with
`agent=react, tools=atlas` and `get_model("mockllm/model", custom_outputs=[...])`
that calls `atlas overview`, `atlas expand t1`, a bash `atlas grep`, writes
`/work/report.md` and submits, with `-T min_runtime_fraction=0` and
`score=False`. The sample's `atlas_coverage` metadata should list each call.

## 6. Gap-checker pilot (atlas with `gapcheck`)

The atlas prompt paragraph now asks the agent to run `atlas gapcheck` on its
draft before finalising (Fix items: correct; Consider items: optional). The
automatic variant also runs it once, on the first turn after 60% of the budget
(when a draft exists), and sends the output with the same framing; its
outcome is in sample metadata as `gapcheck_auto`.

Arm A, atlas with on-demand `gapcheck`:

```sh
uv run inspect eval messageboard_audit_bench/german_wiki_report \
  -T agent=react -T tools=atlas -T time_limit_minutes=10 -T policy_aware_continue=true \
  --model openrouter/z-ai/glm-5.3 --model-role grader=openai/gpt-6.1-sol \
  -M provider='{"order": ["wafer"], "allow_fallbacks": false}' \
  --time-limit 2400 --epochs 3 --max-samples 3 \
  --no-fail-on-error --score-on-error \
  --log-dir logs/atlas-gap-ondemand
```

Arm B, plus the automatic run at 60%:

```sh
uv run inspect eval messageboard_audit_bench/german_wiki_report \
  -T agent=react -T tools=atlas -T time_limit_minutes=10 -T policy_aware_continue=true \
  -T gapcheck_at=0.6 \
  --model openrouter/z-ai/glm-5.3 --model-role grader=openai/gpt-6.1-sol \
  -M provider='{"order": ["wafer"], "allow_fallbacks": false}' \
  --time-limit 2400 --epochs 3 --max-samples 3 \
  --no-fail-on-error --score-on-error \
  --log-dir logs/atlas-gap-auto
```

Run both at the same time. Compare with each other, with pilot 5 (atlas
before the gap checker) and baseline 5. Look at all four scores, the
per-finding split (missed / partial / full), and whether Fix items get
corrected (rerun `atlas gapcheck` on the final reports).
