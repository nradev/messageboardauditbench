"""Entities: the values an investigator pivots on, with when and by whom they were seen.

Two sources, both generic:
  * structured values: each value of a short identifier/category field (``table.field``),
    skipping ids, text, times and numbers;
  * extracted values: hosts, IPs, paths, env-var names and e-mail addresses found inside any
    string field (URLs in request fields count as much as URLs in post bodies).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from .signals import IPV4, PATH, URL, hosts

EMAIL = re.compile(r"\b[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63})+\b")
# Environment variables as they are used, not every upper-case word: $NAME, ${NAME},
# NAME=value at a word start, export NAME, getenv("NAME"), environ["NAME"].
ENV_REF = re.compile(
    r"\$\{?([A-Z][A-Z0-9_]{2,})\}?|(?:^|[\s;&|(])([A-Z][A-Z0-9]*_[A-Z0-9_]+)=|\bexport\s+([A-Z][A-Z0-9_]{2,})|"
    r"(?:getenv|environ(?:\.get)?)\s*[(\[]\s*['\"]([A-Z][A-Z0-9_]{2,})['\"]",
    re.M,
)
EXTRACTED = ("host", "ip", "path", "env", "email")
_MAX_TEXT = 20000


@dataclass
class EntityStat:
    rows: int = 0
    first: datetime | None = None
    last: datetime | None = None
    tables: Counter = field(default_factory=Counter)
    actors: Counter = field(default_factory=Counter)
    first_ref: tuple[str, int] | None = None


def extract(text: str) -> dict[str, set[str]]:
    sample = text[:_MAX_TEXT]
    out: dict[str, set[str]] = {k: set() for k in EXTRACTED}
    for m in URL.finditer(sample):
        try:
            host = urlsplit(m.group(0) if "://" in m.group(0) else "http://" + m.group(0)).hostname
        except ValueError:
            host = None
        if host:
            out["host"].add(host.lower())
    out["host"] |= hosts(sample)
    out["ip"] = set(IPV4.findall(sample))
    out["host"] -= out["ip"]
    out["path"] = {p for p in PATH.findall(sample) if len(p) > 3}
    out["env"] = {next(g for g in m if g) for m in ENV_REF.findall(sample)}
    out["email"] = set(EMAIL.findall(sample))
    return out


def structured_fields(profile) -> list[str]:
    """Fields whose values are entities themselves: short strings that repeat."""
    out = []
    for f in profile.fields.values():
        if f.role not in ("actor", "category", "other") or f.types.get("str", 0) < 0.9 * max(f.present, 1):
            continue
        if f.distinct < 2 or f.mean_len > 120 or f.distinct > 0.5 * f.present:
            continue  # constants, long strings and near-unique keys are not pivot values
        out.append(f.name)
    return out


def build_entities(idx) -> dict[tuple[str, str], EntityStat]:
    stats: dict[tuple[str, str], EntityStat] = {}

    def add(key, table, row, when, actor):
        s = stats.get(key)
        if s is None:
            s = stats[key] = EntityStat()
        s.rows += 1
        s.tables[table] += 1
        if actor:
            s.actors[actor] += 1
        if when is not None:
            if s.first is None or when < s.first:
                s.first, s.first_ref = when, (table, row)
            if s.last is None or when > s.last:
                s.last = when
        elif s.first_ref is None:
            s.first_ref = (table, row)

    for name, t in idx.tables.items():
        p = idx.profiles[name]
        sfields = structured_fields(p)
        strings = [f for f, fs in p.fields.items() if fs.types.get("str") and fs.role not in ("time",)]
        for i, r in enumerate(t.rows):
            when = idx.time_of(name, i)
            actor_pairs = idx.actor_of(name, i)
            actor = f"{actor_pairs[0][0]}={actor_pairs[0][1]}" if actor_pairs else ""
            for f in sfields:
                v = r.get(f)
                if isinstance(v, str) and v:
                    add((f"{name}.{f}", v), name, i, when, actor)
            seen: set[tuple[str, str]] = set()
            for f in strings:
                v = r.get(f)
                if not isinstance(v, str) or len(v) < 4:
                    continue
                for kind, vals in extract(v).items():
                    for x in vals:
                        seen.add((kind, x))
            for key in seen:
                add(key, name, i, when, actor)
    return stats
