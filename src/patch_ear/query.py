"""Query layer used by both the CLI and the MCP server. Read-only."""

from __future__ import annotations

import numpy as np

from .features import EXTRA, FEATURES

SORTABLE = FEATURES + EXTRA + ["grit", "weight"]


def _value(row: dict, key: str) -> float:
    return row["pct"][key] if key in ("grit", "weight") else row["features"][key]


def summarize(row: dict, full: bool = False) -> dict:
    out = {
        "id": row["id"], "name": row["name"], "library": row.get("library", ""), "category": row["category"], "source": row["source"],
        "tags": row.get("tags", []), "labels": row["labels"],
        "grit_pct": round(row["pct"]["grit"]), "weight_pct": round(row["pct"]["weight"]),
        "centroid_hz": round(row["features"]["centroid_hz"]),
        "duration_s": round(row["features"]["duration_s"], 1),
    }
    if full:
        out["path"] = row["path"]
        out["n_samples"] = row.get("n_samples", 1)
        out["measured_as"] = {3: "average of 3 notes (an octave below, at, and above the reference note)", 2: "average of its samples", 1: "one sample of several"}.get(row["representative"], "its only sample")
        out["features"] = {k: round(v, 4) for k, v in row["features"].items()}
        out["percentiles_in_category"] = {k: round(v) for k, v in row["pct"].items()}
    return out


def search(rows, filters=None, sort_by="grit", descending=True, limit=15, tag=None, library=None):
    """filters: {feature_or_score: [min|None, max|None]} on raw feature values (scores are 0-100)."""
    for key in (filters or {}):
        if key not in SORTABLE:
            raise ValueError(f"unknown filter '{key}'. Valid: {SORTABLE}")
    if sort_by not in SORTABLE:
        raise ValueError(f"unknown sort_by '{sort_by}'. Valid: {SORTABLE}")
    out = []
    for r in rows:
        if tag and tag.lower() not in (t.lower() for t in r.get("tags", [])):
            continue
        if library and library.lower() not in r.get("library", "").lower():
            continue
        ok = True
        for key, (lo, hi) in (filters or {}).items():
            v = _value(r, key)
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                ok = False
                break
        if ok:
            out.append(r)
    out.sort(key=lambda r: _value(r, sort_by), reverse=descending)
    return out[:limit]


def similar(rows, target_id: str, n=10):
    """Nearest neighbours by z-scored feature distance across all rows."""
    mat = np.array([[r["features"][f] for f in FEATURES] for r in rows])
    z = (mat - mat.mean(axis=0)) / (mat.std(axis=0) + 1e-9)
    ids = [r["id"] for r in rows]
    if target_id not in ids:
        raise ValueError(f"unknown id '{target_id}'")
    d = np.linalg.norm(z - z[ids.index(target_id)], axis=1)
    return [(rows[i], float(d[i])) for i in d.argsort() if ids[i] != target_id][:n]
