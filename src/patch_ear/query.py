"""Query layer used by both the CLI and the MCP server. Read-only."""

from __future__ import annotations

import numpy as np

from .descriptors import NAMES as TRAINED
from .features import EXTRA, FEATURES

SORTABLE = FEATURES + EXTRA + ["grit", "weight", "intensity"] + TRAINED


def _value(row: dict, key: str) -> float | None:
    """Raw value of a measurement; None when this sound has none (the trained scores exist only for sounds the sidecar has scored)."""
    if key in ("grit", "weight"):
        return row["pct"][key]
    if key == "intensity":
        return row["pct"].get("intensity")
    if key in TRAINED:
        return row.get("trained", {}).get(key)
    return row["features"][key]


def summarize(row: dict, full: bool = False) -> dict:
    out = {
        "id": row["id"], "name": row["name"], "library": row.get("library", ""), "category": row["category"], "source": row["source"],
        "tags": row.get("tags", []), "labels": row["labels"],
        "grit_pct": round(row["pct"]["grit"]), "weight_pct": round(row["pct"]["weight"]),
        **({"intensity_pct": round(row["pct"]["intensity"])} if "intensity" in row["pct"] else {}),
        "centroid_hz": round(row["features"]["centroid_hz"]),
        "duration_s": round(row["features"]["duration_s"], 1),
    }
    if row.get("trained"):
        t = row["trained"]
        out["arousal"], out["valence"] = t.get("arousal"), t.get("valence")
        out["timbre"] = {k[7:]: v for k, v in t.items() if k.startswith("timbre_")}
    if row.get("mood") or row.get("use"):
        out["mood"], out["use"] = row.get("mood", []), row.get("use", [])
    if full:
        out["mood_scores"] = row.get("mood_scores", {})
        out["trained"] = row.get("trained", {})
        out["trained_percentiles_in_category"] = {k: round(v) for k, v in row.get("trained_pct", {}).items()}
        out["path"] = row["path"]
        out["n_samples"] = row.get("n_samples", 1)
        out["measured_as"] = {3: "average of 3 notes (an octave below, at, and above the reference note)", 2: "average of its samples", 1: "one sample of several"}.get(row["representative"], "its only sample")
        out["features"] = {k: round(v, 4) for k, v in row["features"].items()}
        out["percentiles_in_category"] = {k: round(v) for k, v in row["pct"].items()}
    return out


def search(rows, filters=None, sort_by="grit", descending=True, limit=15, tag=None, library=None, mood=None, use=None):
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
        if mood and not all(m.lower() in r.get("mood", []) for m in ([mood] if isinstance(mood, str) else mood)):
            continue
        if use and not all(u.lower() in r.get("use", []) for u in ([use] if isinstance(use, str) else use)):
            continue
        if library and library.lower() not in r.get("library", "").lower():
            continue
        ok = True
        for key, (lo, hi) in (filters or {}).items():
            v = _value(r, key)
            if v is None or (lo is not None and v < lo) or (hi is not None and v > hi):
                ok = False
                break
        if ok:
            out.append(r)
    missing = float("-inf") if descending else float("inf")  # sounds without the measurement go last
    out.sort(key=lambda r: missing if _value(r, sort_by) is None else _value(r, sort_by), reverse=descending)
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
