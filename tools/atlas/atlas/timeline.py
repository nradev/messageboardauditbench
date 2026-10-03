"""Time structure: when activity starts and ends, where it peaks, where it changes level,
and where it goes quiet. Per file, and per value of each short category field.

Everything is counted in one time unit chosen from the data: the finest unit in UNITS that
the timestamps resolve and that splits the corpus span into at most MAX_UNITS buckets. A
nine-hour transcript is read in 10-minute buckets, a two-month log in days, several years
in weeks, so the same rules find structure at any scale.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .index import Index

MAX_CATEGORY_VALUES = 10  # category fields with more values are not split into series
MAX_UNITS = 150  # the unit is the finest that splits the corpus span into at most this many
UNITS = [("minute", 60), ("10 minutes", 600), ("hour", 3600), ("6 hours", 21600), ("day", 86400),
         ("week", 7 * 86400)]
_RESOLUTION = {"sub-second": 1, "second": 1, "minute": 60, "hour": 3600, "day": 86400}


@dataclass(frozen=True)
class Unit:
    name: str
    seconds: int

    def floor(self, t: datetime) -> datetime:
        if self.seconds >= 7 * 86400:
            d = t.replace(hour=0, minute=0, second=0, microsecond=0)
            return d - timedelta(days=d.weekday())
        if self.seconds >= 86400:
            return t.replace(hour=0, minute=0, second=0, microsecond=0)
        day = t.replace(hour=0, minute=0, second=0, microsecond=0)
        offset = int((t - day).total_seconds()) // self.seconds * self.seconds
        return day + timedelta(seconds=offset)

    def label(self, t: datetime) -> str:
        if self.seconds >= 7 * 86400:
            return f"week of {t.date().isoformat()}"
        if self.seconds >= 86400:
            return t.date().isoformat()
        return t.strftime("%Y-%m-%d %H:%M")

    def span_label(self, a: datetime, b: datetime) -> str:
        """``a`` to the end of ``b``'s bucket, compactly."""
        end = b + timedelta(seconds=self.seconds)
        if self.seconds >= 86400:
            return f"{self.label(a)} → {self.label(b)}"
        if a.date() == end.date() or (end - timedelta(seconds=1)).date() == a.date():
            return f"{self.label(a)}–{end.strftime('%H:%M')}"
        return f"{self.label(a)} → {self.label(end)}"

    @property
    def sub_day(self) -> bool:
        return self.seconds < 86400

    @property
    def one(self) -> str:
        return {"10 minutes": "10-minute bucket", "6 hours": "6-hour bucket"}.get(self.name, self.name)

    @property
    def plural(self) -> str:
        return {"minute": "minutes", "10 minutes": "10-minute buckets", "hour": "hours",
                "6 hours": "6-hour buckets", "day": "days", "week": "weeks"}.get(self.name, self.name)


DAY = Unit("day", 86400)


def time_unit(idx: Index) -> Unit:
    times = [(fs.tmin, fs.tmax, fs.precision.get("resolution", "second"))
             for p in idx.profiles.values() if p.time_field
             for fs in [p.fields[p.time_field]] if fs.tmin and fs.tmax]
    if not times:
        return DAY
    span = (max(t[1] for t in times) - min(t[0] for t in times)).total_seconds()
    finest = min(_RESOLUTION.get(t[2], 1) for t in times)
    for name, sec in UNITS:
        if sec >= finest and span / sec <= MAX_UNITS:
            return Unit(name, sec)
    return Unit(*UNITS[-1])


@dataclass
class Event:
    kind: str  # start | end | peak | rise | fall | quiet
    series: str  # "events" or "events.event_type=delete"
    at: datetime  # start of the bucket
    end_at: datetime | None = None  # last bucket of a quiet stretch
    count: int = 0
    share: float = 0.0  # of the series' rows in that bucket
    note: str = ""


@dataclass
class Series:
    name: str
    rows: int
    unit: Unit = DAY
    counts: dict[datetime, int] = field(default_factory=dict)  # bucket start -> rows


def series_for(idx: Index, unit: Unit | None = None) -> list[Series]:
    unit = unit or time_unit(idx)
    out = []
    for table, p in idx.profiles.items():
        if not p.time_field:
            continue
        counts: Counter = Counter()
        by_value: dict[tuple[str, str], Counter] = {}
        cats = [f for f, fs in p.fields.items() if fs.role == "category" and 2 <= fs.distinct <= MAX_CATEGORY_VALUES]
        rows = idx.tables[table].rows
        for i in range(len(rows)):
            t = idx.time_of(table, i)
            if not t:
                continue
            b = unit.floor(t)
            counts[b] += 1
            for f in cats:
                v = rows[i].get(f)
                if isinstance(v, str) and v:
                    by_value.setdefault((f, v), Counter())[b] += 1
        if counts:
            out.append(Series(table, sum(counts.values()), unit, dict(counts)))
        for (f, v), c in sorted(by_value.items(), key=lambda kv: -sum(kv[1].values())):
            total = sum(c.values())
            if total >= max(20, 0.02 * sum(counts.values())):
                out.append(Series(f"{table}.{f}={v}", total, unit, dict(c)))
    return out


def events_for(s: Series) -> list[Event]:
    if not s.counts:
        return []
    step = timedelta(seconds=s.unit.seconds)
    first, last = min(s.counts), max(s.counts)
    n = int((last - first) / step) + 1
    buckets = [first + k * step for k in range(n)]
    counts = [s.counts.get(b, 0) for b in buckets]
    total = sum(counts) or 1
    ev = [Event("start", s.name, first, count=s.counts[first]), Event("end", s.name, last, count=s.counts[last])]
    # Peaks: the busiest buckets, if they stand out from the typical active bucket.
    active = sorted(x for x in counts if x)
    median = active[len(active) // 2] if active else 0
    for k in sorted(range(n), key=lambda k: -counts[k])[:3]:
        if counts[k] >= max(3 * median, 10) and counts[k] / total >= 0.05:
            ev.append(Event("peak", s.name, buckets[k], count=counts[k], share=counts[k] / total))
    # Level changes: compare the mean of the 3 buckets before and after each bucket.
    if n >= 7:
        best_rise = best_fall = None
        for k in range(3, n - 2):
            before = sum(counts[k - 3 : k]) / 3
            after = sum(counts[k : k + 3]) / 3
            if after >= 3 * max(before, 1) and after - before >= 10:
                if best_rise is None or after - before > best_rise[1]:
                    best_rise = (k, after - before)
            if before >= 3 * max(after, 1) and before - after >= 10:
                if best_fall is None or before - after > best_fall[1]:
                    best_fall = (k, before - after)
        for kind, best in (("rise", best_rise), ("fall", best_fall)):
            if best:
                ev.append(Event(kind, s.name, buckets[best[0]], count=int(best[1]),
                                note=f"the mean over 3 {s.unit.plural} changes by about {best[1]:.0f} rows per "
                                     f"{s.unit.one}"))
    # Quiet stretches: 3+ consecutive empty buckets inside the active span.
    k = 0
    while k < n:
        if counts[k] == 0:
            j = k
            while j < n and counts[j] == 0:
                j += 1
            if j - k >= 3:
                ev.append(Event("quiet", s.name, buckets[k], end_at=buckets[j - 1]))
            k = j
        else:
            k += 1
    return ev


def describe(e: Event, unit: Unit) -> str:
    when = unit.label(e.at)
    if e.kind == "start":
        return f"{e.series}: first activity {when}"
    if e.kind == "end":
        return f"{e.series}: last activity {when}"
    if e.kind == "peak":
        what = f"{when}–{(e.at + timedelta(seconds=unit.seconds)).strftime('%H:%M')}" if unit.sub_day else when
        return f"{e.series}: peak {'at' if unit.sub_day else 'on'} {what} ({e.count:,} rows, {e.share:.0%} of the series)"
    if e.kind in ("rise", "fall"):
        return f"{e.series}: sharp {e.kind} around {when} ({e.note})"
    return f"{e.series}: no activity {unit.span_label(e.at, e.end_at)}"


def cmd_timeline(idx: Index, args) -> str:
    from . import coverage
    from .fmt import footer

    unit = time_unit(idx)
    out = [f"Time structure (start, end, peaks, sharp rises/falls, quiet stretches) per file and per category "
           f"value, counted per {unit.name} (chosen from the corpus span and timestamp precision):"]
    for s in series_for(idx, unit):
        evs = events_for(s)
        if not evs:
            continue
        out.append(f"\n{s.name}: {s.rows:,} rows over {len(s.counts)} active {unit.plural}")
        out += ["  " + describe(e, unit).split(": ", 1)[1] for e in evs]
    out.append(footer(f"atlas count TABLE --by {'hour' if unit.sub_day else 'day'}", "atlas pivot VALUE"))
    coverage.record("timeline", [])
    return "\n".join(out)
