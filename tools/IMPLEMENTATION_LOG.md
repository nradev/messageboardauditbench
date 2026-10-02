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
