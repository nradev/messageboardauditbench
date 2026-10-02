"""Load a directory of JSONL, CSV or plain-text log files into flat rows.

Every row gets a short reference ``<file-stem>:<line>`` (1-based line number) that the
other commands print and accept. Nested values are flattened with dotted keys; lists
of scalars are kept as lists.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED = (".jsonl", ".ndjson", ".json", ".csv", ".tsv", ".log", ".txt")


@dataclass
class Table:
    name: str  # file stem, used in refs
    path: Path
    rows: list[dict] = field(default_factory=list)


def _flatten(obj, prefix="", out=None):
    out = {} if out is None else out
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            _flatten(v, key + ".", out)
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            # Lists of records (e.g. diff lines): join their scalar values into one text value.
            parts = []
            for item in v:
                parts.append(" ".join(str(x) for x in item.values() if isinstance(x, (str, int, float))))
            out[key] = "\n".join(parts)
        else:
            out[key] = v
    return out


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                rows.append({})
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                obj = {"text": line}
            rows.append(_flatten(obj) if isinstance(obj, dict) else {"value": obj})
    return rows


def _load_json(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", errors="replace") as f:
        obj = json.load(f)
    if isinstance(obj, dict):
        # A single object wrapping a list of records is common; take the longest list.
        lists = [v for v in obj.values() if isinstance(v, list)]
        obj = max(lists, key=len) if lists else [obj]
    return [_flatten(o) if isinstance(o, dict) else {"value": o} for o in obj]


def _load_csv(path: Path, delimiter: str) -> list[dict]:
    with path.open(encoding="utf-8", errors="replace", newline="") as f:
        return [dict(r) for r in csv.DictReader(f, delimiter=delimiter)]


def _load_text(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", errors="replace") as f:
        return [{"text": line.rstrip("\n")} for line in f]


def load_table(path: Path) -> Table:
    suffix = path.suffix.lower()
    if suffix in (".jsonl", ".ndjson"):
        rows = _load_jsonl(path)
    elif suffix == ".json":
        rows = _load_json(path)
    elif suffix == ".csv":
        rows = _load_csv(path, ",")
    elif suffix == ".tsv":
        rows = _load_csv(path, "\t")
    else:
        rows = _load_text(path)
    return Table(name=path.stem, path=path, rows=rows)


def data_files(data_dir: Path) -> list[Path]:
    files = []
    for root, _dirs, names in os.walk(data_dir, followlinks=True):
        for n in sorted(names):
            p = Path(root) / n
            if p.suffix.lower() in SUPPORTED and not n.startswith("."):
                files.append(p)
    return sorted(files)


def load_dir(data_dir: Path) -> list[Table]:
    tables = [load_table(p) for p in data_files(data_dir)]
    # Disambiguate stems that repeat across subdirectories.
    seen: dict[str, int] = {}
    for t in tables:
        seen[t.name] = seen.get(t.name, 0) + 1
    for t in tables:
        if seen[t.name] > 1:
            t.name = str(t.path.relative_to(data_dir).with_suffix("")).replace(os.sep, "/")
    return tables
