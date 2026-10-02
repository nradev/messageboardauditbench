# atlas

A map of a log corpus for an investigating agent: compress the corpus into clusters of
near-duplicate records, show the rare and unusual ones first, expand any of them on
request, and keep track of what has already been looked at.

Pure Python standard library (the benchmark sandbox has no third-party packages and no
network). Works on any directory of `.jsonl`, `.json`, `.csv`, `.tsv`, `.log` or `.txt`
files; nothing in it knows about a particular dataset.

```sh
tools/atlas/bin/atlas --data data/verbatim overview
ATLAS_DATA=data/verbatim tools/atlas/bin/atlas grep -i 'timeout|retry'
```

| command | what it does |
|---|---|
| `overview` | files, guessed field roles, per text field the biggest clusters (the gist) and the most salient small ones |
| `profile [TABLE]` | per field: role, counts, top and rare values, time range and timestamp precision |
| `clusters [--field T.F] [--sort salience\|size\|time] [--windows]` | list clusters (`cNN`) or windows (`wNN`), paginated |
| `expand ID` | open a cluster or window: span, actors, per-day counts, varied members; later members show only lines that differ |
| `show REF` | one row in full (`table:line`, 1-based) |
| `grep PATTERN [-i]` | regex search; hits grouped by cluster, small salient clusters first with context, big clusters one line each |
| `unseen` | coverage so far and the most salient clusters not yet opened |

Every response ends with `→ next:` and the exact commands worth running next.
`--json` gives machine-readable cluster lists. Field guesses can be overridden with
`--time-field`, `--actor-field` and `--text-field`.

## How it works

- **Fields** (`profile.py`): roles are inferred from value shapes (time, id, text,
  category, actor) per file, with field names only as a tie-breaker.
- **Clusters** (`cluster.py`): each text field separately. Values of up to 12 tokens go
  through a small Drain-style template miner; longer ones through one-permutation
  MinHash (word 4-shingles, 64 bins) with LSH and leader clustering, in time order, so
  each cluster's first member is its earliest occurrence.
- **Windows** (`index.py`): files whose text barely compresses (mostly unique rows, such
  as a single transcript) are also split into windows of 20 consecutive rows.
- **Salience** (`signals.py`): rarity × content richness, from generic signals only:
  length, URLs/hosts, IPs, paths, environment variables, shell commands, code,
  mixed-script tokens.
- **Coverage** (`coverage.py`): every command appends to `$ATLAS_STATE`; `unseen` reads
  it back, and the benchmark stores it in sample metadata as a process metric.
- The index is cached under `$ATLAS_CACHE`, keyed on the data files and the atlas code.

`validate_clusters.py DATA_DIR` prints the generic clustering diagnostics (size
distribution, homogeneity, fragmentation) used to set and freeze the thresholds.

## In the benchmark

`-T tools=atlas` (with `agent=react`, `backend=inspect`) copies the package into the
sandbox at sample start, adds an `atlas` tool for the agent, puts `atlas` on PATH for
bash, and records the coverage log as `atlas_coverage` in the sample metadata. Without
the option nothing changes.

```sh
uv run inspect eval messageboard_audit_bench/german_wiki_report \
  -T agent=react -T tools=atlas -T time_limit_minutes=30 --model openrouter/...
```

Design and history: `tools/TOOL_IDEAS.md`, `tools/IMPLEMENTATION_LOG.md`.
