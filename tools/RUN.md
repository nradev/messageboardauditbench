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

## 7. 30-minute pair: full atlas vs baseline, 2 epochs each

Tests whether a longer budget leaves room for depth: more drilling into what
atlas surfaces, and time to act on gap-check items. The atlas arm is the full
current tool (atlas, the `gapcheck` prompt sentence, and the automatic gap
check at 60%, which is minute 18). The baseline is the published condition
plus `policy_aware_continue`, as before.

`--time-limit 3600`: the sample limit covers the agent's 30 minutes and
scoring, and Inspect gives scoring half of it, so 60 min leaves 30 for the 8
sequential sheets and enough headroom for the agent's 30. Run both at the same
time.

Atlas, full:

```sh
uv run inspect eval messageboard_audit_bench/german_wiki_report \
  -T agent=react -T tools=atlas -T time_limit_minutes=30 -T policy_aware_continue=true \
  -T gapcheck_at=0.6 \
  --model openrouter/z-ai/glm-5.3 --model-role grader=openai/gpt-6.1-sol \
  -M provider='{"order": ["wafer"], "allow_fallbacks": false}' \
  --time-limit 3600 --epochs 2 --max-samples 2 \
  --no-fail-on-error --score-on-error \
  --log-dir logs/atlas-30min
```

Baseline:

```sh
uv run inspect eval messageboard_audit_bench/german_wiki_report \
  -T agent=react -T time_limit_minutes=30 -T policy_aware_continue=true \
  --model openrouter/z-ai/glm-5.3 --model-role grader=openai/gpt-6.1-sol \
  -M provider='{"order": ["wafer"], "allow_fallbacks": false}' \
  --time-limit 3600 --epochs 2 --max-samples 2 \
  --no-fail-on-error --score-on-error \
  --log-dir logs/baseline-30min
```

Read them as in section 4 (`usage_metrics.py` on each log dir). With 2 epochs
per arm this shows direction, not significance. Compare the four scores, the
per-finding split, report words and calls per run against the 10-minute pair
(pilot 5 / baseline 5), and the `gapcheck_auto` metadata (did Fix items go
away; which Consider items were taken up).

## 8. Reading crew (`crew_brief`, `crew_ask`, `crew_sweep`, `crew_notes`)

**Reader check (a few cents).** Before any pilot, look at real reader output:
are notes specific, do quotes verify, how long does a call take. Run it on
all three corpora. Pick theme ids from `atlas overview` for each corpus.

```sh
uv run python tools/crew_check.py --data data/verbatim t1 t5 grep:proxy \
  --model openrouter/z-ai/glm-5.3 --provider wafer
uv run python tools/crew_check.py --data data/mythos5 w3 w10 \
  --model openrouter/z-ai/glm-5.3 --provider wafer
uv run python tools/crew_check.py --data data/rubyhack t1 \
  --model openrouter/z-ai/glm-5.3 --provider wafer
```

`ask` instead of `brief`:

```sh
uv run python tools/crew_check.py --data data/verbatim grep:proxy \
  --ask "Which proxy or relay services did agents use, and do any records report whether they worked?" \
  --model openrouter/z-ai/glm-5.3 --provider wafer
```

`sweep` (the corpus's most informative records, no target):

```sh
for d in verbatim mythos5 rubyhack; do
  uv run python tools/crew_check.py --data data/$d sweep --model openrouter/z-ai/glm-5.3 --provider wafer
done
```

**atlas,crew only (10 minutes, 3 epochs).** Compare against earlier atlas runs
rather than a new atlas arm (see the caveat below):

```sh
uv run inspect eval messageboard_audit_bench/german_wiki_report \
  -T agent=react -T tools=atlas,crew -T time_limit_minutes=10 -T policy_aware_continue=true \
  --model openrouter/z-ai/glm-5.3 --model-role grader=openai/gpt-6.1-sol \
  -M provider='{"order": ["wafer"], "allow_fallbacks": false}' \
  --time-limit 2400 --epochs 3 --max-samples 3 \
  --no-fail-on-error --score-on-error \
  --log-dir logs/crew-pilot
```

Caveat: the closest earlier atlas arm is pilot 5 (`logs/atlas-pilot5`), which
predates the gap-check sentence in the atlas prompt, and it ran on a
different day (provider speed varies). A difference is then crew plus that
sentence plus day-to-day noise.

**atlas,crew, 30 minutes (matches `logs/atlas-30min`: gapcheck at 60%, 2 epochs),
with the background sweep:**

```sh
uv run inspect eval messageboard_audit_bench/german_wiki_report \
  -T agent=react -T tools=atlas,crew -T time_limit_minutes=30 -T policy_aware_continue=true \
  -T gapcheck_at=0.6 -T sweep_at_start=true \
  --model openrouter/z-ai/glm-5.3 --model-role grader=openai/gpt-6.1-sol \
  -M provider='{"order": ["wafer"], "allow_fallbacks": false}' \
  --time-limit 3600 --epochs 2 --max-samples 2 \
  --no-fail-on-error --score-on-error \
  --log-dir logs/crew-30min
```

Since step 17, tool arms also get an exploration hint in the early-finish
messages, so this arm differs from `atlas-30min` by the crew, the background
sweep and that hint.

**Pilot (after the reader check).** The arms differ only by `,crew`. Run
them at the same time. Readers use the agent's model unless you add
`--model-role reader=...` (record it if you do). Reader tokens are in each
sample's `crew` metadata and are also billed as normal model usage.

```sh
for arm in atlas atlas,crew; do
uv run inspect eval messageboard_audit_bench/german_wiki_report \
  -T agent=react -T tools=$arm -T time_limit_minutes=10 -T policy_aware_continue=true \
  --model openrouter/z-ai/glm-5.3 --model-role grader=openai/gpt-6.1-sol \
  -M provider='{"order": ["wafer"], "allow_fallbacks": false}' \
  --time-limit 2400 --epochs 3 --max-samples 3 \
  --no-fail-on-error --score-on-error \
  --log-dir logs/crew-pilot-${arm/,/-} &
done; wait
```

A third arm adds the background sweep at the start (`-T sweep_at_start=true`
with `tools=atlas,crew`): the digest arrives on the first turn after it
finishes. Its timing is in `crew_sweep_at_start`.

```sh
uv run inspect eval messageboard_audit_bench/german_wiki_report \
  -T agent=react -T tools=atlas,crew -T sweep_at_start=true -T time_limit_minutes=10 \
  -T policy_aware_continue=true \
  --model openrouter/z-ai/glm-5.3 --model-role grader=openai/gpt-6.1-sol \
  -M provider='{"order": ["wafer"], "allow_fallbacks": false}' \
  --time-limit 2400 --epochs 3 --max-samples 3 \
  --no-fail-on-error --score-on-error \
  --log-dir logs/crew-pilot-sweep
```

Read the results with `usage_metrics.py` as in section 4. It also prints the
crew calls, reader tokens, the share of quotes that verified, and rows read by
readers.

## 9. Final writer (`-T writer=W1|W2|W3`)

**Offline check first (a few cents).** Run the writer on a finished sample's
report and read the result: does it keep the draft's findings, are the
citations right, does it pass its own checks?

```sh
uv run python tools/writer_check.py logs/atlas-tok200k-v0.3.0/<file>.eval --sample 0 \
  --level W3 --data data/verbatim \
  --model openrouter/z-ai/glm-5.3 --provider wafer --out /tmp/writer-final.md
```

**Paired arms, run at the same time.** Each writer level is compared with the
same arm without a writer, with 3 runs each. The writer's reserve (10% by
default) comes out of the same 200k total, so the agent gets 180k.

```sh
for w in none W1 W3; do
uv run inspect eval messageboard_audit_bench/german_wiki_report \
  -T agent=react -T config=blind-tokens -T token_budget=200000 \
  -T tools=atlas -T gapcheck_at=0.6 $([ $w != none ] && echo "-T writer=$w") \
  --model openrouter/z-ai/glm-5.3 --model-role grader=openai/gpt-6.1-sol \
  -M provider='{"order": ["wafer"], "allow_fallbacks": false}' \
  --epochs 3 --max-samples 3 \
  --no-fail-on-error --score-on-error \
  --log-dir logs/atlas-tok200k-writer-$w &
done; wait
```

**Variants in one run (cheaper).** One agent run per sample, several writers
graded side by side. The agent keeps its whole 200k, so the main scores are the
no-writer condition. Each variant gets its own 20k on top (`writer_reserve`
sets the size).

```sh
uv run inspect eval messageboard_audit_bench/german_wiki_report \
  -T agent=react -T config=blind-tokens -T token_budget=200000 \
  -T tools=atlas -T gapcheck_at=0.6 \
  -T writer_variants=W3:edit,W3:rebalance,W3:rewrite \
  --model openrouter/z-ai/glm-5.3 --model-role grader=openai/gpt-6.1-sol \
  -M provider='{"order": ["wafer"], "allow_fallbacks": false}' \
  --epochs 3 --max-samples 3 \
  --no-fail-on-error --score-on-error \
  --log-dir logs/atlas-tok200k-variants

uv run python tools/writer_variants_metrics.py logs/atlas-tok200k-variants
```

Grading cost grows with each variant (one more findings and TL;DR grading per
sample).

For crew arms, use W2 or W3 with `-T tools=atlas,crew -T sweep_at_start=true`.
`-T writer_strength=rebalance` or `rewrite` lets the writer depart further from
the draft (default `edit`); `tools/writer_check.py --strength` tries them offline.
The outcome is in each sample's `writer` metadata. When the writer replaced the
report, the draft is in `writer_draft_report`, so the draft and the final report
can be graded side by side.
