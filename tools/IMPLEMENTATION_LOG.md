# Implementation log

How the build of the tools in `tools/` went, step by step. The plan is in
`TOOL_IDEAS.md`; this file records what happened, and **where the design or
approach had to change relative to that plan** (marked **Change**).

Branch `claude/atlas`, worktree `.worktrees/claude-atlas`.

## 2026-10-02: setup

- **Data.** `scripts/build_data.sh` could not reach `collusion.wiki` from this
  machine (connection timeout; other hosts fine). The Wayback Machine has the
  archive (`web.archive.org/web/20260904112141id_/...full-wiki-logs.zip`). It
  matched the pinned SHA256 exactly and every variant reproduced
  `data/SHA256SUMS.variants`, so it is the benchmark dataset. Mythos 5 and
  RubyHack corpora built locally from tracked inputs.
- **Worktree helper.** `scripts/worktree_add.sh` failed to link the data
  variants because its inline `python3` lacks `inspect_ai`; the links were made
  by hand. Not fixed here (unrelated to the tools); worth a small fix on `main`.

## Constraint: standard library only

- **Change.** The sandbox image is `python:3.12-slim` with no extra Python
  packages and no network inside the container. A tool the agent runs from
  `bash` must therefore be pure standard-library Python (or be added to the
  image, which would change the image for every condition). The plan said to
  use `drain3` for template mining; instead `atlas` has a small Drain-style
  miner of its own (`atlas/cluster.py`, ~40 lines). MinHash is also hand-rolled.
- **Change.** To make pure-Python MinHash fast enough, it uses
  *one-permutation hashing* (one hash per shingle, 64 bins, rotation
  densification) instead of 64 independent hash functions. Index build on the
  wiki corpus (42k rows, bodies up to 38k chars) takes about 3.5s, so it can run
  at agent start or be cached.

## Step 1: `profile` with field inference

- Roles inferred from value shapes: time (≥90% parse as ISO-8601 or epoch), id
  (near-unique, complete), text (long), category (few values, each frequent),
  actor (identifier-like, moderate cardinality). Field names are only a
  tie-breaker, using generic log vocabulary (user, ip, host, author, label...).
- Timestamp precision is reported per time field (resolution, share on
  midnight / on the hour / on the minute, duplicates).
- Tested on all three local corpora (wiki JSONL, Mythos 5 transcript, RubyHack
  package diffs). Two rules misfired on the wiki and were fixed:
  - sparse fields with few values (e.g. a field present on 24 of 19,931 rows)
    were called categories; a category now needs each value to recur (≥5 rows
    per distinct value on average);
  - long strings without spaces (URLs) were called "other"; any field with mean
    length ≥40 is now text.
- Known weakness: "actor" candidates include page/entity keys as well as real
  actors; the name tie-breaker puts real actors (ip, label) first on this
  corpus. Overrides (`--actor-field`) exist for when it is wrong.

## Step 2: clustering and offline validation

- Each text field is clustered on its own. Values of ≤12 tokens go to the
  Drain-style miner; longer ones to MinHash + LSH with **leader clustering**
  (a value joins the most similar leader, never a member, so drifting variants
  cannot chain into one cluster). Rows are processed in time order, so each
  cluster's leader is its earliest occurrence, which also answers "who did it
  first".
- `tools/atlas/validate_clusters.py` reports only generic diagnostics: size
  distribution, homogeneity (exact Jaccard of random member pairs in big
  clusters), fragmentation (best exact Jaccard of sampled singletons to any
  other leader).
- Wiki `revisions.body`: 14,514 values → 4,909 clusters; 18.6% of values are
  singletons. Homogeneity median 0.80, p10 0.47, no pair below 0.2.
  Fragmentation: 8% of singletons have a leader at Jaccard ≥0.5 but **0% at
  ≥0.7**, i.e. only borderline cases near the threshold. Changing LSH bands
  (16×4, 21×3, 32×2, 128-bin variants) barely moved any number, so LSH recall
  is not the issue. **Thresholds frozen** at K=64, 16×4 bands, join at 0.5,
  before looking at where any specific finding lands.
- **Bug found by validation:** cluster counts differed between runs because
  Python's `hash()` is randomized per process. Switched to two seeded
  `zlib.crc32` calls (64 bits, C speed). Runs are now identical.
- **Sanity check on held-out corpora (not used for tuning).**
  - RubyHack: 129 diff records → 62 clusters, homogeneous.
  - **Change (design finding).** Mythos 5, a single agent transcript:
    98% of `content` values and 43% of `tool_result` values are singletons.
    Clustering compresses a corpus with heavy repetition (many actors posting
    similar things) but **not a single sequential narrative**. The plan treated
    clusters as the universal unit for overview and coverage. `atlas` needs a
    fallback unit for low-redundancy fields: consecutive windows in time or
    row order. Decision: keep clusters as the unit where they compress, and
    have `unseen` and the overview report windows when a field's singleton
    share is high.
- Scale note: 4,909 clusters for one field is far more than an agent can scan.
  The first screen therefore has to depend on salience ranking, which is
  exactly the "first screen" rule from review; a hierarchy of clusters (the
  Clio idea) may become necessary rather than optional.

## Step 3: commands (`overview`, `profile`, `clusters`, `expand`, `show`, `grep`, `unseen`)

- Salience = rarity × content richness from generic signals (length, URLs/hosts,
  IPs, paths, env-var names, shell commands, code, mixed-script tokens). On the
  wiki corpus the top small clusters are long technical posts; on Mythos 5 it
  **saturates**: every 20-row window of a technical transcript has every
  signal. **Change:** windows are listed in order for now; ranking them needs
  a different signal (novelty, from `timeline --novel`), not more content
  signals.
- Problems found by reading the output as an agent would, and fixed:
  - Templates were shown lowercased; matching is now case-insensitive but
    display keeps the original case.
  - Singletons were shown as their masked template (`https <*> …`); masked
    templates are now only shown for clusters with more than one member.
  - **Boilerplate ate the snippets** (a wiki's default first line on many
    posts). Generic fix: lines occurring in >1% of a field's values are dropped
    from snippets (never from `expand`/`show`).
  - **`expand` repeated near-identical members in full** (4 × 1,500 chars).
    **Change:** later members are shown as a line diff against the first one
    shown ("1 line added: …"). On a 112-member cluster this cut the output from
    ~6k characters to ~1.8k with no loss.
  - The index cache was keyed on the data files only, so a stale pickled index
    survived a code change. The key now includes a hash of the atlas source.
- `grep` puts small, salient clusters first with ±220 chars of context, then
  big clusters one line each, then hits in non-text fields as value counts.
  Functional check with a generic word: the first screen showed long, unique
  technical posts ahead of 75 big repetitive clusters. (This check used a word
  related to a known finding; it confirms the ordering works and was not used
  to tune anything.)
- Field-inference fix found by the tests: a `user` field with only 6 distinct
  values was classed as a category, leaving the file without an actor. An
  identifier-like field with an actor-style name now stays an actor.
- Timing: index build 3.5–6s (wiki corpus), cached calls ~0.2–0.5s.

## Step 4: benchmark integration (`-T tools=atlas`, react only)

- **Change (no image change).** The plan assumed adding the tool to the image.
  Instead the package is copied into the sandbox at sample start, so the image
  and baseline runs stay byte-identical; the option is off by default and
  rejected for other agents/backends.
- **Change (no root).** The first version installed to `/opt` and
  `/usr/local/bin` as root. The benchmark sandbox deliberately permits only
  uid 1000 (`messageboard_audit_bench/sandbox.py`), so this failed, correctly.
  atlas now lives in `~/.local/share/atlas` with a wrapper in `~/.local/bin`,
  added to PATH via `~/.profile` (Inspect's bash tool runs `bash --login`).
- The agent learns about atlas from an **`atlas` tool** whose description holds
  the command list; prompt bytes are unchanged. It is also on PATH in bash. The
  tool's output passes through the same time-budget/report-length feedback
  wrapper as `bash` and `text_editor`.
- The index is built once during setup (6.2s in a `python:3.12-slim`
  container with no network, uid 1000, read-only data), and the coverage log
  is cleared so it only records the agent's own calls. At the end it is stored
  as `atlas_coverage` in sample metadata.
- Sample ids get a `+atlas` suffix, and `investigation_tools` is in sample
  metadata, so tool runs cannot be mixed up with baseline runs.
- **Verified at no cost:** the real `german_wiki_report` task with a scripted
  mock model (`atlas overview`, `atlas expand`, `atlas grep` from bash, write
  report, submit) completed successfully in the real sandbox; all three calls
  appear in `atlas_coverage`.
- Tests: `tests/test_atlas.py` (6 tests on synthetic data). `ruff` clean. The
  full suite has 2 failures in `tests/test_share_site.py` that also fail on
  `main`; unrelated to this work.

## Step 5: telling the agent to use atlas (before the smoke pilot)

- **Change.** The plan relied on the tool description alone so that prompt
  bytes stayed identical across conditions. On review that tests "does a model
  discover an unannounced tool" as much as "does the tool help", and the
  auditing-agent work shows agents under-use tools they are not pointed to.
  With `tools=atlas`, the task prompt now ends with one paragraph
  (`TOOL_PROMPTS` in `messageboard_audit_bench/investigation_tools.py`) saying
  how to use atlas: start with `overview`, drill in with
  `expand`/`grep`/`show`, run `unseen` periodically and before finalising. It
  says nothing about what to look for, so it is the same for any incident.
  The tool description also now says "Start with `overview`".
- The baseline prompt is unchanged (checked: the atlas prompt is the baseline
  prompt plus 489 characters). The added text is stored as
  `investigation_tools_prompt` in sample metadata.
- Caveat for interpreting results: the conditions now differ by tool *and* by
  an instruction. Some of any gain could come from the instruction itself
  (e.g. "look for what you have not seen yet"). If the gain is large, a
  control with the instruction's generic advice but no tool would separate the
  two.

## Step 6: fixes from implementation review (before the smoke pilot)

A second agent reviewed v0.1 against real output. Applied:

- **Domain word in the tool's own text.** The example ref in the tool
  description and help was `revisions:120`, a table name from this dataset,
  which broke the no-domain-words rule exactly where it matters most. Now
  `logs:120`.
- **Change: salience weights signals by rarity within the field.** Each signal
  gets an IDF-like weight in [0, 1]: present in ≥50% of a field's clusters → 0,
  in ≤1% → 1. Previously every signal counted the same, so signals carried by
  nearly every record (URL and env-var names in link-heavy posts) inflated
  scores without telling records apart.
- **Low-information penalty.** Richness is multiplied by a compressibility
  factor (zlib ratio of the first 4k chars; texts under 200 bytes exempt).
  A 140-repeat `hello aaaa…` post dropped from about 6th to 2,704th in
  `unseen`.
- **`code` false positives.** Bare `public `/`private `/`static `/`return` at
  line start matched English prose ("public OCR paths…"). Access modifiers now
  need a following type/keyword; `return` needs a statement ending in `;`.
- **`grep` ignored numbers.** Only string values were searched, so "no
  matches" silently meant "no string matches". Ints and floats are now
  searched as text.
- **Coverage args for `clusters`** came from `sys.argv`, which is wrong after
  the options-before-command reordering (and under pytest). Now built from the
  parsed arguments. This matters because coverage is the process metric.
- **`profile` noise on near-unique fields.** The `rare:` list is suppressed
  when ≥90% of values are distinct (it was just arbitrary last rows), and
  `top:` prints "all values distinct" instead of a list of ×1 values.
- **Boilerplate in `grep` contexts.** Snippets now use the boilerplate-free
  text when the match survives there.
- Removed dead `Index.units()`.

Not changed, by decision:
- **Windows on single transcripts are still unranked.** Weighting cannot help
  when every window carries every signal; on Mythos 5 the per-message
  clusters still favour long technical messages. The real fix is novelty
  ranking (`timeline --novel`).
- **Partial duplicates** (the same block reposted inside different
  wrappers) stay separate clusters; the reviewer checked that MinHash tracks
  exact Jaccard (0.14–0.41) on such cases, so this is correct behaviour, not
  calibration error. A containment measure is a possible later addition.
- **`anomalies` and `entities`** are the biggest gaps (nothing else can
  surface look-alike identifiers unless the agent already suspects them). They
  come after the smoke pilot, which tests adoption, and before any graded
  comparison.
- Docs (`README.md` layout, `messageboard_audit_bench/README.md` task options)
  to be updated once the tool settles.

## Smoke pilot 1 (GLM 5.3, react, 10 min, tools=atlas)

Log: `logs/atlas-smoke/2026-10-02T20-19-49-00-00_german-wiki-report_nT99q5gdHDg234ZE4UJGUB.eval`.

- **Ran cleanly; scoring did not finish.** Report written (2,992 words).
  Inspect gives scoring half of the sample time limit (`time_limit / 2`
  in `inspect_ai/_eval/task/run.py`): (10 + 5 min guard) / 2 = 450s. The
  sheet scorer grades the 8 `v2` sheets **sequentially**; with
  `openai/gpt-6-astra` each took 50–73s, so the 8th was cancelled and `tldrh`
  never ran. Round 4 avoided this by scoring after generation
  (`score_during_generation = false`). Not an atlas problem.
- **Adoption: yes, but only for orientation.** 11 of 83 tool calls were atlas,
  all within the first ~80s of tool activity: `overview`, `profile` ×3,
  `expand` of the top two salient clusters, two `atlas grep`s, one `unseen`,
  two `show`s. After that, 43 bash calls (mostly ad-hoc Python over the JSONL)
  and 29 report edits; atlas was never used again, and `unseen` was not run
  before finalising despite the prompt.
- **The intended mechanism did fire once:** the agent expanded the top
  salient singleton from `overview`, then ran an `atlas grep` built from terms
  it found there.
- **What it went to bash for:** structured, non-text analysis: delete-event
  counts and sequences, per-IP/label relationships, referrers, request
  payload decoding, per-minute rates. atlas has no commands for these yet;
  they map onto `entities`/`pivot`, `anomalies` (bursts) and `timeline`.
- atlas output was 1.8k–10k chars per call (~51k chars over 11 calls);
  `grep` and `overview` are the largest.
- The agent wrote its first report draft at ~48s of tool activity and spent
  most of the run editing it, so there was little exploration time in which
  `unseen` would have mattered.

## Step 7: changes from the smoke pilot (refs, examples, diversity, entities, pivot, count, metrics)

Prioritised from what the pilot agent did in bash (filter-and-count scripts,
one value across files in time order, joins, decoding, identifier scans), not
from which known findings it missed. A reviewer read the run against the
answer key; we kept the conclusions that also follow from behaviour alone.

- **References.** Rows now print `file:line (idfield=value)`, e.g.
  `events:7 (event_id=request:dse:2026-05-24:3)`; `show` also accepts the
  record's own id; help and tool description say lines are 1-based and valid
  in shell/python. The pilot agent spent several calls checking this.
- **`profile` shows an example record per file** (the first of the
  most-complete rows, values shortened). The pilot agent ran `head -c` on
  every file right after `profile`.
- **Change: diversity (MMR) in `overview`, `clusters`, `grep` and `unseen`.**
  First version used word-set Jaccard between leaders; it barely changed page
  1 (mean similarity 0.06 → 0.05) because long posts on the same topic share
  few words. **Switched to overlap of mid-frequency "topic words"** (document
  frequency 2–15% of the 300-item pool), which separates topics clearly
  (same-topic pairs 0.5–0.67, unrelated pairs mostly <0.1). Page 1 of `unseen`
  now spans ~12 topics instead of 3–4, keeps 9 of the plain top 15, and still
  starts with the highest-scoring item; cost 0.07s.
- **Coverage framed against the top salient set.** `unseen` now reports
  "opened k/50 of the most salient small clusters" before the total over all
  ~6,200 small clusters, which reads as hopeless.
- **New: `entities`.** Values of short repeated identifier/category fields
  (`table.field`) plus hosts, IPs, paths, env-var names and e-mail addresses
  extracted from any string field; per value: rows, first/last seen, top
  actors. Summary shows each kind's commonest values and rarest ones;
  `--kind K --sort rare|count|first` pages through one kind.
  - Fields with >50% distinct values (foreign keys like a per-row reference)
    are excluded: they produced 14,591 meaningless "entities".
  - **Change: hosts outside URLs need a country-code or common generic TLD.**
    `name.ext` tokens (`wiki.cgi`, `window.location`, `123.xlsx`) were being
    counted as hosts. Hosts inside URLs are always kept.
  - **Change: env names only when used as variables** (`$NAME`, `${NAME}`,
    `NAME=…`, `export NAME`, `getenv`/`environ`). Upper-case words in prose
    were flooding the list. Consequence: the wiki corpus now has no env
    entities (its posts mention such names only in prose); Mythos 5 has 16
    real ones. We deliberately did **not** add a suffix list (`_PROXY`,
    `_KEY`, …) that would restore the wiki's mentions, because that would
    mainly serve one known finding; `grep` still finds them.
  - Bug found by tests: kinds with a single value were hidden from the
    summary, which is exactly the long tail on small corpora. Fixed.
- **New: `pivot VALUE`.** Every row in any file and field containing the value
  (substring, case-insensitive; `--exact` for whole values), with which
  fields matched, co-occurring actors/categories, per-day counts, then one
  merged timeline (25 per page). Rows show only short actor/category values,
  skipping values that repeat one already shown (page / wiki/page /
  wiki~page) and the pivot value itself. 0.6s on the wiki corpus.
- **New: `count TABLE[.FIELD]`** with `--where F=V | F!=V | F~REGEX`
  (repeatable, AND) and `--by day|hour|month|FIELD`. Top values, tail summary
  (how many values occur once), and per-value buckets. The footer suggests
  pivoting on the *rarest* top value, not the commonest.
- **Prompt and tool description** now mention `entities`, `pivot` and `count`
  ("for questions about one value or one field … before writing a script").
- **`tools/atlas/usage_metrics.py`**: per sample, tool calls by function,
  atlas calls by subcommand and share, first/last atlas call (s and % of tool
  activity), `unseen` count and last time, clusters opened, coverage of the
  top-50 salient set (with `--data`), report words, scores. On smoke pilot 1:
  11/91 calls (12%), last atlas call at 18% of tool activity, 1 `unseen` at
  13%, 3/50 top salient opened (against today's ranking).
- Costs: index build on the wiki corpus went from ~6s to ~11–13s (entity
  extraction). It runs during setup, inside the budget's wall clock but
  before the agent's own limit starts, so the agent's time-left notes read
  ~13s (~2% of 10 min) low. Calls take 0.6–0.8s (larger pickled index).
- Verified: 13 atlas tests pass; ruff clean; full suite only has the 2
  pre-existing `test_share_site.py` failures; scripted mock run in the real
  sandbox exercised `overview`, `expand`, `entities`, `pivot`, `count`, `grep`.
