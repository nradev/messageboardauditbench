# atlas

A map of a log corpus for an investigating agent: compress the corpus into clusters of
near-duplicate records and topic-level themes, show the rare and unusual ones first,
expand any of them on request, answer the small structured questions that would otherwise
need a script, check a draft report against the data, and keep track of what has already
been looked at.

Pure Python standard library (the benchmark sandbox has no third-party packages and no
network). Works on any directory of `.jsonl`, `.json`, `.csv`, `.tsv`, `.log` or `.txt`
files; nothing in it knows about a particular dataset.

```sh
tools/atlas/bin/atlas --data data/verbatim overview
ATLAS_DATA=data/verbatim tools/atlas/bin/atlas grep 'timeout|retry'
```

## Commands

Orientation and reading:

| command | what it does |
|---|---|
| `overview` | start here: files, field roles, themes (what is typical, with topic words and actors), the biggest repeated records, the richest rare records (each says whether it belongs to a theme or is isolated) |
| `themes [--field T.F]` | topics shared by many records and actors (`tNN`), most actors first |
| `profile [TABLE]` | per field: role, counts, top and rare values, time range and timestamp precision, an example record |
| `clusters [--field T.F] [--sort salience\|size\|time] [--windows]` | list clusters of near-duplicate values (`cNN`) or windows (`wNN`), paginated |
| `expand ID [--n N]` | open a theme, cluster or window: span, actors, counts per time unit, varied members; later members of a cluster show only the lines that differ |
| `show REF [--offset N]` | one row in full; REF is `file:line` (1-based, also valid in shell and Python), the record's own id, or a cluster/theme id (its first record) |
| `grep PATTERN [-i] [--field T.F]` | regex search (smart case; numbers searchable): hits grouped by cluster, small salient clusters first with context, big clusters one line each |
| `unseen` | what has not been looked at: rare records never shown first, then those shown only as a listing line, then those read only by crew readers; top unopened themes and repeated records; coverage of the 50 most salient rare records. Each call moves on |

Structured questions (instead of writing a script):

| command | what it does |
|---|---|
| `entities [--kind K] [--sort rare\|count\|first]` | values to pivot on: field values, and hosts, IPs, paths, env variables and e-mails extracted from text, rarest first, with first/last seen and actors |
| `pivot VALUE [--exact]` | every row in any file containing VALUE, as one timeline, with co-occurring actors and categories |
| `count TABLE[.FIELD] [--where ...] [--by day\|hour\|FIELD]` | filtered counts and group-bys; `--where` takes `F=V`, `F!=V`, `F~REGEX`, `F>V`, `F<V`, `F>=V`, `F<=V` (numeric or text order, so ISO times work) |
| `rows TABLE [--where ...] [--fields a,b] [--sort time\|FIELD] [--desc]` | matching rows, one line each, with their ids |
| `join A.FIELD B.FIELD [-i]` | which values of one field appear in another (overlap, examples) |

Structure, oddities and the report:

| command | what it does |
|---|---|
| `timeline` | per file and per category value: first and last activity, peaks, sharp rises and falls, quiet stretches, counted in a time unit chosen from the data |
| `anomalies` | look-alike identifiers (values that differ only by confusable characters, e.g. a Cyrillic letter in a name), mixed-script words, actor bursts, bursts of one repeated record; each list capped |
| `gapcheck [REPORT] [--dismiss gID ...]` | check a draft report against the data. **Fix**: citations and quotes the data does not support as written (bad refs, quotes not in the cited record, ids that exist nowhere); held to near-zero false positives. **Consider**: optional, phrased as questions (themes, dated events, files never mentioned, thin passages, material already surfaced but unused); leaving out immaterial items is correct |

Used by the reading crew (below), not listed in `--help`:

| command | what it does |
|---|---|
| `records SET [--limit N] [--chars N] [--where ...]` | a set of records as JSON for readers; SET is `tNN`, `cNN`, `wNN`, `grep:REGEX`, `pivot:VALUE`, `rows:TABLE`, `around:REF`, `refs:A,B` or `sweep` |
| `mark REF ... [--label L]` | log rows as read by crew readers (`unseen` lists them last) |

Every response ends with `→ next:` and the commands worth running next. Options may come
before or after the command. `--json` gives machine-readable cluster lists (and gapcheck
items). Field guesses can be overridden with `--time-field`, `--actor-field` and
`--text-field`.

## How it works

- **Fields** (`profile.py`): roles inferred from value shapes (time, id, text, category,
  actor) per file, with field names only as a tie-breaker; timestamp precision is
  measured, so day-precision or placeholder times are not read as exact.
- **Clusters** (`cluster.py`): each text field separately. Values of up to 12 tokens go
  through a Drain-style template miner; longer ones through one-permutation MinHash (word
  4-shingles, 64 bins, banded LSH) with leader clustering in time order, so a cluster's
  first member is its earliest occurrence. Hashing is deterministic (crc32).
- **Windows** (`index.py`): files whose text barely compresses (one long transcript) are
  also split into windows of 20 consecutive rows.
- **Salience** (`signals.py`): rarity × content richness, from generic signals only
  (length, compressibility, URLs/hosts, IPs, paths, env variables, shell commands, code,
  mixed-script tokens), each weighted by how rare it is in its field.
- **Themes** (`themes.py`): topic words with moderate document frequency seed themes,
  groups of clusters about the same thing across many actors; rare records say whether
  they relate to a theme. Listings are diversified (MMR on topic words).
- **Entities** (`entities.py`): values of short fields plus hosts, IPs, paths, env
  variables (as used, not every capitalised word) and e-mails extracted from any text.
- **Time unit** (`timeline.py`): the finest of minute, 10 minutes, hour, 6 hours, day and
  week that the timestamps resolve and that splits the corpus span into at most 150
  buckets (a transcript of hours is read in 10-minute buckets, a two-month log in days).
  `timeline`, `expand`, actor bursts and gapcheck's time items all use it; gapcheck
  matches report times of day for sub-day units.
- **Look-alikes** (`anomalies.py`, `confusables.py`): each short field value is reduced to
  its Unicode TR39 confusable skeleton; values sharing a skeleton but not their raw form,
  with a plain-ASCII original present, are flagged. The table (non-ASCII → ASCII, 2,245
  entries) is vendored from Unicode's `confusables.txt`; regenerate it with
  `gen_confusables.py`.
- **Excerpts** (`records.py`): a record over its character budget is not cut at the head:
  short fields stay whole, long values keep their first lines plus the most informative
  others (lines rare across clusters of that field, with content signals or rare hosts,
  IPs, paths), in order, with markers where lines were left out.
- **Gap check** (`gapcheck.py`): quotes are matched against the corpus after
  normalisation (case, quotes, dashes, Markdown, elisions); citation pairing follows
  sentence and bracket structure. Two Consider slots are reserved for material the agent
  already had (unused reader notes, look-alikes, disguised mixed-script words).
- **Coverage** (`coverage.py`): every command appends to `$ATLAS_STATE` what it opened,
  listed and showed in full; crew reads and gapcheck dismissals go there too, and reader
  notes to a file next to it (`$ATLAS_NOTES`). `unseen` and `gapcheck` read them back; the
  benchmark stores the log in sample metadata as a process metric.
- The index is cached under `$ATLAS_CACHE`, keyed on the data files and the atlas code.

Helper scripts:

- `validate_clusters.py DATA_DIR`: clustering diagnostics (size distribution, homogeneity,
  fragmentation) used to set and freeze the thresholds.
- `usage_metrics.py LOG.eval [--data DIR]`: per sample, tool and atlas usage, coverage,
  crew calls and reader tokens, the four headline metrics, errors.
- `gen_confusables.py confusables.txt`: regenerates `atlas/confusables.py`.

## In the benchmark

`-T tools=atlas` (with `agent=react`, `backend=inspect`) copies the package into the
sandbox at sample start, builds the index, adds an `atlas` tool for the agent, puts
`atlas` on PATH for bash, appends one paragraph to the task prompt, and records the
coverage log as `atlas_coverage` in sample metadata. Without the option nothing changes.

Related task options (all off by default, so default runs are the published condition):

| option | effect |
|---|---|
| `-T tools=atlas,crew` | also the reading crew (below) |
| `-T policy_aware_continue=true` | before the earliest acceptable finish, Inspect's "call submit()" nudge is replaced by one that asks to keep investigating; reaching the early-completion cap accepts the report and sets `minimum_runtime_violation` instead of failing the sample. With tools enabled, both early-finish messages also name the arm's tools for widening the search |
| `-T gapcheck_at=0.6` | runs `atlas gapcheck` on the draft once, on the first turn after that share of the budget, and sends its output with the "corrections first; Consider items optional" framing; outcome in `gapcheck_auto` |
| `-T sweep_at_start=true` | (needs `crew`) starts a crew sweep when the agent starts and hands its digest over on the first turn after it finishes; timing in `crew_sweep_at_start` |

```sh
uv run inspect eval messageboard_audit_bench/german_wiki_report \
  -T agent=react -T tools=atlas,crew -T time_limit_minutes=30 -T policy_aware_continue=true \
  --model openrouter/... --model-role grader=...
```

Full run commands: `tools/RUN.md`.

## Reading crew

`messageboard_audit_bench/reading_crew.py`, enabled with `-T tools=atlas,crew`. An agent
can read only a small part of a corpus within its budget; the crew's readers read
records in parallel, outside the agent's turns, and return short notes with citations.

- **Actions** (one `crew` tool):
  - `brief SET`: who, what and how, claims, times, outcomes, anything unexpected and open
    questions in a set, plus a short cited summary (about 60 distinct records).
  - `ask SET question=...`: readers keep the records that bear on the question, each with
    what it says and a quote; a reduce call answers from that evidence; the output says
    how many records were relevant, so a "no" is scoped to what was read (about 120).
  - `sweep`: no target; a cross-section of the corpus picked by atlas (`records sweep`):
    mostly salient rare records, a few theme examples and repeated records, windows for
    narrative files. It skips records already read or opened, units already shown in a
    listing and atlas's top-50 most salient rare records; each record is tagged with why
    it was picked, and calling again moves on (about 150).
  - `notes [REGEX]`: every verified note of the run, those not shown before first.
- **Sets** are atlas's: `tNN`, `cNN`, `wNN`, `grep:REGEX`, `pivot:VALUE`, `rows:TABLE`
  with `where` filters, `around:REF`, `refs:A,B`. Large sets are sampled (the most
  repeated, the most unusual, the rest spread over time); near-duplicates are read once;
  long records are excerpted.
- **Readers** run on the host (the sandbox has no network) as Inspect model calls: the
  `reader` model role if given (`--model-role reader=...`), else the agent's own model, so
  readers add no outside knowledge or stronger reasoning by default. One generic schema;
  prompts forbid outside knowledge, judgements about whether things are real or
  malicious, and statements about the batch itself.
- **Verification**: every note carries a record ref and an exact quote, checked against
  the text that reader was given (the gap checker's normaliser); a quote held by another
  record of the batch is re-attributed only if exactly one record holds it, otherwise the
  note is dropped. Summary sentences must cite records that were read.
- **Caps**: 15 records or 14k characters per reader, 8 readers at once, 60 s per reader
  (one retry for an unusable reply), 90 s per tool call (less near the deadline), 120
  reader calls and 3M tokens per run. Output: at most 4 notes per kind and 20 in total
  (sweep 5 and 25), quotes up to 25 words; the rest is kept for `crew notes`.
- **Coverage and notes**: records read are marked in atlas's coverage log (`unseen`
  lists them last); all verified notes go to `/tmp/atlas-notes.jsonl`, where `gapcheck`
  finds notes the report does not use.
- **Metadata**: `crew` holds per-call stats (set, seconds, readers finished, notes),
  reader calls, tokens, latency, timeouts, errors, quotes returned, verified,
  re-attributed and dropped, and notes kept and shown.
- **Reader check** (small cost, outside an eval): `tools/crew_check.py --data DIR SET ...
  [--ask "question"] --model ... [--provider ...]` prints the outputs and verification
  rates; `sweep` works as a SET.

Design, decisions and results: `tools/TOOL_IDEAS.md`, `tools/IMPLEMENTATION_LOG.md`.
