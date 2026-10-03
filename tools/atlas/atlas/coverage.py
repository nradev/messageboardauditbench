"""Coverage log: which clusters and rows the agent has actually seen.

Every command appends one JSON line to ``$ATLAS_STATE`` (default ``~/.atlas/coverage.jsonl``).
``opened`` lists units (clusters, themes, windows) whose content was shown; ``seen_rows``
lists row refs whose text was shown at length; ``listed`` lists units that appeared as a
one-line entry in some listing (agents often act on those without opening them); ``read``
lists row refs that readers of the reading crew read for the agent. ``unseen`` reads it back, and the same file is the
per-run process metric (how much of the long tail the agent opened).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def state_path() -> Path:
    return Path(os.environ.get("ATLAS_STATE", Path.home() / ".atlas" / "coverage.jsonl"))


def record(cmd: str, args: list[str], opened=(), seen_rows=(), listed=(), read=()) -> None:
    entry = {
        "t": round(time.time(), 3),
        "cmd": cmd,
        "args": args,
        "opened": sorted(set(opened)),
        "seen_rows": sorted(set(seen_rows)),
        "listed": sorted(set(listed)),
    }
    if read:
        entry["read"] = sorted(set(read))
    try:
        p = state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def load_read() -> set[str]:
    """Row refs read by readers of the reading crew (not by the agent itself)."""
    out: set[str] = set()
    try:
        with state_path().open(encoding="utf-8") as f:
            for line in f:
                try:
                    out.update(json.loads(line).get("read", ()))
                except (json.JSONDecodeError, AttributeError):
                    continue
    except OSError:
        pass
    return out


def load_dismissed() -> set[str]:
    """Gap-checker item ids the agent set aside with `atlas gapcheck --dismiss`."""
    out: set[str] = set()
    try:
        with state_path().open(encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("cmd") == "gapcheck-dismiss":
                    out.update(e.get("args", ()))
    except OSError:
        pass
    return out


def load() -> tuple[set[str], set[str], set[str]]:
    """(opened unit ids, row refs seen at length, unit ids shown in any listing)."""
    opened: set[str] = set()
    rows: set[str] = set()
    listed: set[str] = set()
    try:
        with state_path().open(encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                opened.update(e.get("opened", ()))
                rows.update(e.get("seen_rows", ()))
                if isinstance(e.get("listed"), list):  # older logs stored only a count
                    listed.update(e["listed"])
    except OSError:
        pass
    return opened, rows, listed
