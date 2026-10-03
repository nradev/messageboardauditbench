"""Build (and cache) the corpus index: tables, profiles, clusters, row-to-cluster map."""

from __future__ import annotations

import hashlib
import os
import pickle
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__
from .cluster import Cluster, cluster_field
from .entities import build_entities
from .load import Table, data_files, load_dir
from .profile import TableProfile, parse_time, profile_table
from .signals import salience, signal_set, signal_weights
from .themes import TopicIndex, build_topics

WINDOW = 20  # rows per window
TOP_SALIENT = 50  # coverage is reported against this many most salient small clusters
LOW_REDUNDANCY = 0.5  # a field whose values are mostly singletons gets windows too


@dataclass
class Index:
    data_dir: Path
    tables: dict[str, Table]
    profiles: dict[str, TableProfile]
    clusters: list[Cluster]
    windows: list[Cluster] = field(default_factory=list)
    order: dict[str, list[int]] = field(default_factory=dict)  # table -> rows in time order
    by_id: dict[str, Cluster] = field(default_factory=dict)
    row_cluster: dict[tuple[str, str, int], str] = field(default_factory=dict)  # (table, field, row) -> cid
    overrides: dict = field(default_factory=dict)
    boilerplate: dict[tuple[str, str], set[str]] = field(default_factory=dict)  # (table, field) -> lines
    id_lookup: dict[str, tuple[str, int]] = field(default_factory=dict)  # record id value -> (table, row)
    entities: dict = field(default_factory=dict)  # (kind, value) -> EntityStat
    top_salient: list[str] = field(default_factory=list)  # ids of the most salient small clusters
    topics: dict[tuple[str, str], TopicIndex] = field(default_factory=dict)  # (table, field) -> topics
    themes: list = field(default_factory=list)  # Theme, across fields, in ranked order
    cluster_themes: dict[str, list[str]] = field(default_factory=dict)  # cid -> theme ids containing it

    def native_id(self, table: str, row: int) -> str | None:
        f = self.profiles[table].id_field
        v = self.tables[table].rows[row].get(f) if f else None
        return str(v) if v not in (None, "") else None

    def display_text(self, c: Cluster, row: int) -> str:
        """Text for snippets: lines that recur across >1% of the field's values are dropped."""
        text = self.text_of(c, row)
        bp = self.boilerplate.get((c.table, c.field))
        if not bp:
            return text
        kept = [ln for ln in text.splitlines() if ln.strip() not in bp]
        return "\n".join(kept) if any(x.strip() for x in kept) else text

    def time_of(self, table: str, row: int) -> datetime | None:
        tf = self.profiles[table].time_field
        return parse_time(self.tables[table].rows[row].get(tf)) if tf else None

    def text_of(self, c: Cluster, row: int) -> str:
        r = self.tables[c.table].rows[row]
        if c.kind == "window":
            fields = self.profiles[c.table].text_fields
            return "\n".join(str(r[f]) for f in fields if r.get(f) not in (None, ""))
        v = r.get(c.field)
        return v if isinstance(v, str) else str(v)

    def actor_of(self, table: str, row: int) -> list[tuple[str, str]]:
        r = self.tables[table].rows[row]
        return [(f, str(r[f])) for f in self.profiles[table].actor_fields[:2] if r.get(f) not in (None, "")]


def _fingerprint(data_dir: Path, overrides: dict) -> str:
    h = hashlib.sha256(f"{__version__}|{sorted(overrides.items())}".encode())
    for src in sorted(Path(__file__).parent.glob("*.py")):  # code changes invalidate the cache
        h.update(src.read_bytes())
    for p in data_files(data_dir):
        st = p.stat()
        h.update(f"{p}|{st.st_size}|{st.st_mtime_ns}".encode())
    return h.hexdigest()[:16]


def cache_dir() -> Path:
    return Path(os.environ.get("ATLAS_CACHE", Path.home() / ".cache" / "atlas"))


def build_index(data_dir: Path, overrides: dict | None = None, use_cache: bool = True) -> Index:
    """``overrides`` may set ``time_field``, ``actor_field`` and ``text_field`` (comma-separated
    lists allowed for text), applied to every table that has that field."""
    overrides = {k: v for k, v in (overrides or {}).items() if v}
    data_dir = data_dir.resolve()
    path = cache_dir() / f"index-{_fingerprint(data_dir, overrides)}.pkl"
    if use_cache and path.exists():
        try:
            with path.open("rb") as f:
                return pickle.load(f)
        except Exception:
            pass
    tables = {t.name: t for t in load_dir(data_dir)}
    profiles = {}
    for name, t in tables.items():
        p = profile_table(name, t.rows)
        if overrides.get("time_field") in p.fields:
            p.time_field = overrides["time_field"]
        if overrides.get("actor_field"):
            wanted = [f for f in overrides["actor_field"].split(",") if f in p.fields]
            if wanted:
                p.actor_fields = wanted + [f for f in p.actor_fields if f not in wanted]
        if overrides.get("text_field"):
            wanted = [f for f in overrides["text_field"].split(",") if f in p.fields]
            if wanted:
                p.text_fields = wanted
        profiles[name] = p
    clusters: list[Cluster] = []
    orders: dict[str, list[int]] = {}
    for name, t in tables.items():
        p = profiles[name]
        order = list(range(len(t.rows)))
        if p.time_field:
            far = datetime.max.replace(tzinfo=None)
            keyed = []
            for i, r in enumerate(t.rows):
                dt = parse_time(r.get(p.time_field))
                keyed.append((dt.replace(tzinfo=None) if dt else far, i))
            order = [i for _, i in sorted(keyed)]
        orders[name] = order
        for f in p.text_fields:
            clusters += cluster_field(name, f, t.rows, order)
    # Stable ids: by table, field, size (largest first), then first row.
    clusters.sort(key=lambda c: (c.table, c.field, -c.size, c.leader))
    order_pos = {name: {r: k for k, r in enumerate(order)} for name, order in orders.items()}
    idx = Index(data_dir, tables, profiles, clusters, overrides=overrides, order=orders)
    for n, c in enumerate(clusters, 1):
        c.cid = f"c{n}"
        idx.by_id[c.cid] = c
        for r in c.members:
            idx.row_cluster[(c.table, c.field, r)] = c.cid
        c.signals = signal_set(idx.text_of(c, c.leader))
    # Salience weights each signal by its rarity among the clusters of the same field.
    by_field: dict[tuple[str, str], list[Cluster]] = {}
    for c in clusters:
        by_field.setdefault((c.table, c.field), []).append(c)
    for group in by_field.values():
        weights = signal_weights([c.signals for c in group])
        for c in group:
            c.score = salience(c.size, idx.text_of(c, c.leader), sum(weights[s] for s in c.signals))
    # Boilerplate lines per field (shown in full by expand/show, dropped from snippets).
    for name, t in tables.items():
        for f in profiles[name].text_fields:
            vals = [r.get(f) for r in t.rows if isinstance(r.get(f), str)]
            if len(vals) < 100:
                continue
            lines = Counter(ln for v in vals for ln in {x.strip() for x in v.splitlines()} if ln)
            bp = {ln for ln, n in lines.items() if n > 0.01 * len(vals) and len(ln) < 200}
            if bp:
                idx.boilerplate[(name, f)] = bp
    # Themes and topic words per text field (needs boilerplate, which snippets exclude).
    for (table, fname), group in by_field.items():
        ti = build_topics(idx, group, table, fname, len(idx.themes) + 1)
        idx.topics[(table, fname)] = ti
        for th in ti.themes:
            idx.themes.append(th)
            leaders = sorted((idx.by_id[cid].leader for cid in th.clusters), key=lambda r: order_pos[table][r])
            unit = Cluster(table, fname, leaders, kind="theme")
            unit.cid = th.tid
            idx.by_id[th.tid] = unit
            for cid in th.clusters:
                idx.cluster_themes.setdefault(cid, []).append(th.tid)
    # Windows for tables where some text field barely compresses (e.g. one long transcript).
    for name in tables:
        fields = {}
        for c in clusters:
            if c.table == name:
                n_vals, n_single = fields.get(c.field, (0, 0))
                fields[c.field] = (n_vals + c.size, n_single + (c.size == 1))
        if not any(v >= 50 and s / v > LOW_REDUNDANCY for v, s in fields.values()):
            continue
        order = orders[name]
        for start in range(0, len(order), WINDOW):
            w = Cluster(name, "(window)", order[start : start + WINDOW], kind="window")
            w.cid = f"w{len(idx.windows) + 1}"
            w.signals = signal_set("\n".join(idx.text_of(w, r) for r in w.members))
            idx.windows.append(w)
            idx.by_id[w.cid] = w
    weights = signal_weights([w.signals for w in idx.windows])
    for w in idx.windows:
        text = "\n".join(idx.text_of(w, r) for r in w.members)
        w.score = salience(1, text, sum(weights[s] for s in w.signals))
    for name in tables:
        if profiles[name].id_field:
            for i in range(len(tables[name].rows)):
                nid = idx.native_id(name, i)
                if nid is not None:
                    idx.id_lookup.setdefault(nid, (name, i))
    idx.top_salient = [c.cid for c in sorted((c for c in clusters if c.size <= 5), key=lambda c: -c.score)
                       [:TOP_SALIENT]]
    idx.entities = build_entities(idx)
    if use_cache:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with tmp.open("wb") as f:
                pickle.dump(idx, f, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.replace(path)
        except OSError:
            pass
    return idx
