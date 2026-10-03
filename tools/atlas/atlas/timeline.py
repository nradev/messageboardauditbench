"""Time structure: when activity starts and ends, where it peaks, where it changes level,
and where it goes quiet. Per file, and per value of each short category field."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from .index import Index

MAX_CATEGORY_VALUES = 10  # category fields with more values are not split into series


@dataclass
class Event:
    kind: str  # start | end | peak | rise | fall | quiet
    series: str  # "events" or "events.event_type=delete"
    day: date
    end_day: date | None = None  # for quiet stretches
    count: int = 0
    share: float = 0.0  # of the series' rows on that day (or stretch)
    note: str = ""


@dataclass
class Series:
    name: str
    rows: int
    days: dict[date, int] = field(default_factory=dict)


def series_for(idx: Index) -> list[Series]:
    out = []
    for table, p in idx.profiles.items():
        if not p.time_field:
            continue
        days: Counter = Counter()
        by_value: dict[tuple[str, str], Counter] = {}
        cats = [f for f, fs in p.fields.items() if fs.role == "category" and 2 <= fs.distinct <= MAX_CATEGORY_VALUES]
        rows = idx.tables[table].rows
        for i in range(len(rows)):
            t = idx.time_of(table, i)
            if not t:
                continue
            d = t.date()
            days[d] += 1
            for f in cats:
                v = rows[i].get(f)
                if isinstance(v, str) and v:
                    by_value.setdefault((f, v), Counter())[d] += 1
        if days:
            out.append(Series(table, sum(days.values()), dict(days)))
        for (f, v), c in sorted(by_value.items(), key=lambda kv: -sum(kv[1].values())):
            total = sum(c.values())
            if total >= max(20, 0.02 * sum(days.values())):
                out.append(Series(f"{table}.{f}={v}", total, dict(c)))
    return out


def events_for(s: Series) -> list[Event]:
    if not s.days:
        return []
    first, last = min(s.days), max(s.days)
    span = (last - first).days + 1
    daily = [s.days.get(first + timedelta(k), 0) for k in range(span)]
    total = sum(daily) or 1
    ev = [Event("start", s.name, first, count=s.days[first]), Event("end", s.name, last, count=s.days[last])]
    # Peaks: the busiest days, if they stand out from the typical active day.
    active = sorted(x for x in daily if x)
    median = active[len(active) // 2] if active else 0
    for k in sorted(range(span), key=lambda k: -daily[k])[:3]:
        if daily[k] >= max(3 * median, 10) and daily[k] / total >= 0.05:
            ev.append(Event("peak", s.name, first + timedelta(k), count=daily[k], share=daily[k] / total))
    # Level changes: compare the mean of the 3 days before and after each day.
    if span >= 7:
        best_rise = best_fall = None
        for k in range(3, span - 2):
            before = sum(daily[k - 3 : k]) / 3
            after = sum(daily[k : k + 3]) / 3
            if after >= 3 * max(before, 1) and after - before >= 10:
                if best_rise is None or after - before > best_rise[1]:
                    best_rise = (k, after - before)
            if before >= 3 * max(after, 1) and before - after >= 10:
                if best_fall is None or before - after > best_fall[1]:
                    best_fall = (k, before - after)
        for kind, best in (("rise", best_rise), ("fall", best_fall)):
            if best:
                ev.append(Event(kind, s.name, first + timedelta(best[0]), count=int(best[1]),
                                note=f"3-day mean changes by about {best[1]:.0f} per day"))
    # Quiet stretches: 3+ consecutive empty days inside the active span.
    k = 0
    while k < span:
        if daily[k] == 0:
            j = k
            while j < span and daily[j] == 0:
                j += 1
            if j - k >= 3:
                ev.append(Event("quiet", s.name, first + timedelta(k), end_day=first + timedelta(j - 1)))
            k = j
        else:
            k += 1
    return ev


def describe(e: Event) -> str:
    d = e.day.isoformat()
    if e.kind == "start":
        return f"{e.series}: first activity {d}"
    if e.kind == "end":
        return f"{e.series}: last activity {d}"
    if e.kind == "peak":
        return f"{e.series}: peak on {d} ({e.count:,} rows, {e.share:.0%} of the series)"
    if e.kind in ("rise", "fall"):
        return f"{e.series}: sharp {e.kind} around {d} ({e.note})"
    return f"{e.series}: no activity {d} → {e.end_day.isoformat()}"


def cmd_timeline(idx: Index, args) -> str:
    from . import coverage
    from .fmt import footer

    out = ["Time structure (start, end, peaks, sharp rises/falls, quiet stretches) per file and per category value:"]
    for s in series_for(idx):
        evs = events_for(s)
        if not evs:
            continue
        days = sorted(s.days)
        out.append(f"\n{s.name}: {s.rows:,} rows over {len(days)} active days")
        out += ["  " + describe(e).split(": ", 1)[1] for e in evs]
    out.append(footer("atlas count TABLE --by day", "atlas pivot VALUE"))
    coverage.record("timeline", [])
    return "\n".join(out)
