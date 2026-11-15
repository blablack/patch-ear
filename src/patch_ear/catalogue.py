"""SQLite catalogue: writer for the analyzer, read-only queries for the MCP server."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

from .features import FEATURES

SCHEMA = """
CREATE TABLE IF NOT EXISTS patches (
    id TEXT PRIMARY KEY,          -- source:relative-path
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    category TEXT NOT NULL,
    path TEXT NOT NULL,
    representative INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    features TEXT NOT NULL,       -- JSON
    tags TEXT NOT NULL DEFAULT '[]',   -- JSON list (e.g. Bass, Pad, Drum)
    n_samples INTEGER NOT NULL DEFAULT 1,  -- samples in the preset; features are one sample's or the average of several
    library TEXT NOT NULL DEFAULT ''       -- the library the preset comes from (the first folder under the Configs root, or the source name)
);
CREATE INDEX IF NOT EXISTS idx_patches_category ON patches(category);
"""

# Derived scores are mean percentile ranks *within a category*.
# (feature, invert) -- invert means a LOW value contributes to a high score.
# grit = noise (flatness) + brightness (energy > 2 kHz). Crest factor was dropped: a plain sine/triangle has a low crest
# factor without any distortion (Junos "Sub Only" ranked gritty). Chosen against 12 of the user's own verdicts on bass
# patches (Spearman +0.82 vs +0.68 with crest) -- a small sample, so keep it simple and re-validate on more.
SCORES = {
    "grit": [("flatness", False), ("hi_ratio", False)],
    "weight": [("low_ratio", False), ("centroid_hz", True)],
}


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path, timeout=15)
        conn.executescript(SCHEMA)
        have = {r[1] for r in conn.execute("PRAGMA table_info(patches)")}
        for col, ddl in (("tags", "TEXT NOT NULL DEFAULT '[]'"), ("n_samples", "INTEGER NOT NULL DEFAULT 1"),
                         ("library", "TEXT NOT NULL DEFAULT ''")):
            if col not in have:  # catalogue created by an earlier version
                conn.execute(f"ALTER TABLE patches ADD COLUMN {col} {ddl}")
    conn.row_factory = sqlite3.Row
    return conn


def known(conn: sqlite3.Connection) -> dict[str, tuple[float, int, str]]:
    """id -> (mtime, size, name); a change in any of them means the row must be refreshed."""
    return {r["id"]: (r["mtime"], r["size"], r["name"]) for r in conn.execute("SELECT id, mtime, size, name FROM patches")}


def upsert(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO patches (id,name,source,source_kind,category,path,representative,mtime,size,features,tags,n_samples,library) "
        "VALUES (:id,:name,:source,:source_kind,:category,:path,:representative,:mtime,:size,:features,:tags,:n_samples,:library)",
        {**row, "features": json.dumps(row["features"]), "tags": json.dumps(row.get("tags", [])), "n_samples": row.get("n_samples", 1), "library": row.get("library", "")},
    )


def prune(conn: sqlite3.Connection, source: str, keep_ids: set[str]) -> int:
    stale = [r["id"] for r in conn.execute("SELECT id FROM patches WHERE source=?", (source,)) if r["id"] not in keep_ids]
    conn.executemany("DELETE FROM patches WHERE id=?", [(i,) for i in stale])
    return len(stale)


def _label(p: dict, f: dict, tags: list[str] | None = None) -> list[str]:
    """Words for a patch. Percentile-based where the notion is relative (grit, weight), absolute thresholds elsewhere."""
    tags = tags or []
    single_cycle = "type:wavetable" in tags  # time-based measures are meaningless for a single-cycle wavetable file
    labels = []
    g, w = p["grit"], p["weight"]
    labels.append("gritty" if g >= 75 else "clean" if g <= 25 else "mid-grit")
    labels.append("heavy/dark" if w >= 75 else "light/bright" if w <= 25 else "mid-weight")
    if f["pitch_clarity"] < 0.3:
        labels.append("unpitched")
    else:
        labels.append("noisy" if p["flatness"] >= 80 else "tonal" if p["flatness"] <= 20 else "")
        if f["inharmonic_ratio"] >= 0.3 and f["pitch_clarity"] >= 0.5:
            labels.append("inharmonic")  # bells, metal, FM clang
        if f["harm_tilt_db"] >= 0:
            labels.append("harmonically rich")
    if f["roughness"] >= 0.2:
        labels.append("rough")
    if not single_cycle:
        if f["lfo_rate_hz"] > 0 and f["lfo_depth"] >= 0.1:
            labels.append(f"pulsing {f['lfo_rate_hz']:.1f}Hz")
        if f["timbre_movement"] >= 0.5 and f["duration_s"] >= 1:
            labels.append("evolving")
        if f["sustain_db"] > -3:
            labels.append("sustains")
        elif f["decay_s"] < 0.5:
            labels.append("decays fast")
    labels.append("mono" if f["side_mid_db"] < -40 else "wide" if f["side_mid_db"] > -12 else "stereo")
    labels.append("long" if f["duration_s"] >= 4 else "short" if f["duration_s"] < 1 else "medium")
    return [x for x in labels if x]


def load_ranked(conn: sqlite3.Connection, category: str | None = None) -> list[dict]:
    """All rows (optionally one category) with percentile ranks, scores and labels. Ranks are per category."""
    q, args = "SELECT * FROM patches", ()
    if category:
        q, args = q + " WHERE category=?", (category,)
    rows = [dict(r) | {"features": json.loads(r["features"]), "tags": json.loads(r["tags"]) if "tags" in r.keys() else []}
            for r in conn.execute(q, args)]
    from . import descriptors, mood

    moods = mood.load(conn)
    trained = descriptors.load(conn)
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        m = moods.get(r["id"])
        r["mood"], r["use"], r["mood_scores"] = (m["mood"], m["use"], m["scores"]) if m else ([], [], {})
        r["trained"] = trained.get(r["id"], {})
        by_cat.setdefault(r["category"], []).append(r)
    for group in by_cat.values():
        n = len(group)
        pct = {}
        for feat in FEATURES:
            vals = np.array([r["features"][feat] for r in group])
            order = vals.argsort().argsort()  # rank 0..n-1 (ties broken arbitrarily; fine at this scale)
            pct[feat] = (order / max(n - 1, 1) * 100) if n > 1 else np.full(n, 50.0)
        tp: dict[str, dict[str, float]] = {}
        for name in descriptors.NAMES:
            have = [r for r in group if name in r["trained"]]
            if len(have) > 1:
                vals = np.array([r["trained"][name] for r in have])
                rank = vals.argsort().argsort() / (len(have) - 1) * 100
                for r, pc in zip(have, rank):
                    tp.setdefault(r["id"], {})[name] = float(pc)
        for i, r in enumerate(group):
            r["trained_pct"] = tp.get(r["id"], {})
            p = {feat: float(pct[feat][i]) for feat in FEATURES}
            for name, parts in SCORES.items():
                p[name] = float(np.mean([100 - p[f] if inv else p[f] for f, inv in parts]))
            ti = tp.get(r["id"], {})
            if "arousal" in ti and "mood_aggressive" in ti:  # one calm..aggressive axis: the relaxed head is unreliable, these two agree
                p["intensity"] = (ti["arousal"] + ti["mood_aggressive"]) / 2
            r["pct"] = p
            r["labels"] = _label(p, r["features"], r.get("tags"))
    return rows
