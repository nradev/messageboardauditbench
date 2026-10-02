"""Per-field statistics and role inference (time, actor, text, id, category).

Roles are guesses from value shapes, with field names used only as a tie-breaker.
They are printed at the top of ``atlas profile`` so a wrong guess is visible, and every
command accepts overrides.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$")
_TIME_NAME = re.compile(r"(time|date|timestamp|^ts$|_at$|created|updated|when)", re.I)
_ACTOR_NAME = re.compile(
    r"(user|actor|author|sender|from|ip|host|account|login|label|role|client|src|source|owner|by$)", re.I
)
_ID_NAME = re.compile(r"(^id$|_id$|_key$|_ref$|uuid|record)", re.I)


def parse_time(v) -> datetime | None:
    """Parse ISO-8601 strings and epoch seconds/milliseconds; None if not a time."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        if 9.0e8 < v < 4.0e9:
            return datetime.fromtimestamp(v, tz=UTC)
        if 9.0e11 < v < 4.0e12:
            return datetime.fromtimestamp(v / 1000, tz=UTC)
        return None
    if isinstance(v, str) and 8 <= len(v) <= 35 and _ISO.match(v.strip()):
        s = v.strip().replace(" ", "T").replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


@dataclass
class FieldStats:
    name: str
    present: int = 0  # rows where the key exists and value is not None/""
    types: Counter = field(default_factory=Counter)
    distinct: int = 0
    top: list = field(default_factory=list)  # [(value, count)]
    rare: list = field(default_factory=list)
    mean_len: float = 0.0
    max_len: int = 0
    space_frac: float = 0.0  # share of string values containing whitespace
    time_frac: float = 0.0
    tmin: datetime | None = None
    tmax: datetime | None = None
    precision: dict = field(default_factory=dict)
    role: str = "other"


@dataclass
class TableProfile:
    name: str
    rows: int
    fields: dict[str, FieldStats]
    time_field: str | None = None
    actor_fields: list[str] = field(default_factory=list)
    text_fields: list[str] = field(default_factory=list)
    id_field: str | None = None


def _hashable(v):
    if isinstance(v, list):
        return tuple(_hashable(x) for x in v)
    if isinstance(v, dict):
        return str(v)
    return v


def _precision(times: list[datetime]) -> dict:
    """How exact the timestamps look: round-value shares, duplicates, finest resolution."""
    n = len(times)
    if not n:
        return {}
    midnight = sum(1 for t in times if t.hour == 0 and t.minute == 0 and t.second == 0)
    on_hour = sum(1 for t in times if t.minute == 0 and t.second == 0)
    on_minute = sum(1 for t in times if t.second == 0 and t.microsecond == 0)
    sub_second = sum(1 for t in times if t.microsecond)
    dup = n - len(set(times))
    if sub_second:
        resolution = "sub-second"
    elif on_minute < n:
        resolution = "second"
    elif on_hour < n:
        resolution = "minute"
    elif midnight < n:
        resolution = "hour"
    else:
        resolution = "day"
    return {
        "resolution": resolution,
        "on_midnight": midnight / n,
        "on_hour": on_hour / n,
        "on_minute": on_minute / n,
        "duplicate": dup / n,
    }


def profile_table(name: str, rows: list[dict]) -> TableProfile:
    keys: dict[str, None] = {}
    for r in rows:
        for k in r:
            keys.setdefault(k, None)
    stats: dict[str, FieldStats] = {}
    n = len(rows)
    for k in keys:
        fs = FieldStats(k)
        counter: Counter = Counter()
        lengths = 0
        n_str = 0
        spaced = 0
        times: list[datetime] = []
        for r in rows:
            v = r.get(k)
            if v is None or v == "" or v == []:
                continue
            fs.present += 1
            fs.types[type(v).__name__] += 1
            hv = _hashable(v)
            counter[hv] += 1
            if isinstance(v, str):
                n_str += 1
                lengths += len(v)
                fs.max_len = max(fs.max_len, len(v))
                if any(c.isspace() for c in v[:200]):
                    spaced += 1
            t = parse_time(v)
            if t is not None:
                times.append(t)
        fs.distinct = len(counter)
        common = counter.most_common()
        fs.top = common[:5]
        fs.rare = [x for x in common[::-1][:5] if x[1] == 1] if fs.distinct > 5 else []
        fs.mean_len = lengths / n_str if n_str else 0.0
        fs.space_frac = spaced / n_str if n_str else 0.0
        fs.time_frac = len(times) / fs.present if fs.present else 0.0
        if times and fs.time_frac >= 0.9:
            fs.tmin, fs.tmax = min(times), max(times)
            fs.precision = _precision(times)
        stats[k] = fs
    prof = TableProfile(name=name, rows=n, fields=stats)
    _assign_roles(prof)
    return prof


def _assign_roles(p: TableProfile) -> None:
    n = max(p.rows, 1)
    for fs in p.fields.values():
        if fs.present == 0:
            fs.role = "empty"
        elif fs.time_frac >= 0.9:
            fs.role = "time"
        elif fs.types.get("str", 0) / fs.present < 0.9:
            fs.role = "numeric" if fs.types.get("int", 0) + fs.types.get("float", 0) else "other"
        elif fs.distinct >= 0.95 * fs.present and fs.present >= 0.9 * n and fs.space_frac < 0.3:
            fs.role = "id"
        elif fs.mean_len >= 40:
            # Long values are text even without spaces (URLs, paths, payloads).
            fs.role = "text"
        elif fs.distinct > 1 and fs.space_frac < 0.2 and fs.mean_len <= 64 and _ACTOR_NAME.search(fs.name):
            fs.role = "actor"  # few distinct users is still a user field, not a category
        elif fs.distinct <= 20 and fs.present >= 5 * fs.distinct:
            fs.role = "category"
        elif fs.space_frac < 0.2 and fs.mean_len <= 64:
            fs.role = "actor"  # identifier-like, moderate cardinality: users, IPs, hosts, pages
        else:
            fs.role = "text" if fs.space_frac >= 0.3 else "other"

    times = [f for f in p.fields.values() if f.role == "time"]
    if times:
        # Prefer the most complete, then the most varied, then a time-like name.
        p.time_field = max(times, key=lambda f: (f.present, f.distinct, bool(_TIME_NAME.search(f.name)))).name
    ids = [f for f in p.fields.values() if f.role == "id"]
    if ids:
        p.id_field = max(ids, key=lambda f: (bool(_ID_NAME.search(f.name)), f.present)).name
    actors = [f for f in p.fields.values() if f.role == "actor"]
    # Name hints rank actor-like fields first; fields named like ids/keys go last.
    actors.sort(key=lambda f: (not _ACTOR_NAME.search(f.name), bool(_ID_NAME.search(f.name)), -f.present))
    p.actor_fields = [f.name for f in actors]
    texts = [f for f in p.fields.values() if f.role == "text"]
    texts.sort(key=lambda f: -f.mean_len * f.present)
    p.text_fields = [f.name for f in texts]
