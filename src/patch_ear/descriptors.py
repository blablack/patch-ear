"""Read side of the sidecar's `descriptors` table (trained mood scores + perceptual timbre scores). Read-only, no heavy imports.

The sidecar (sidecar/descriptors.py, Python 3.12) writes the table; this module only flattens it for the query layer.
Names (all raw values): arousal / valence (1..9, MSD-MusiCNN emomusic head), mood_<x> probabilities 0..1 (Essentia Discogs-EffNet heads),
timbre_<x> (AudioCommons timbral_models, ~0..100).
"""

from __future__ import annotations

import json
import sqlite3

MOOD_NAMES = ["aggressive", "relaxed", "sad", "happy", "party", "acoustic", "electronic", "danceable", "instrumental", "tonal"]
TIMBRE_NAMES = ["hardness", "depth", "brightness", "roughness", "warmth", "sharpness", "boominess", "reverb"]
NAMES = ["arousal", "valence", *[f"mood_{n}" for n in MOOD_NAMES], *[f"timbre_{n}" for n in TIMBRE_NAMES]]


def load(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    """{patch id: {name: value}} for sounds the sidecar has scored; empty if it never ran."""
    try:
        rows = conn.execute("SELECT id, data FROM descriptors").fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, dict[str, float]] = {}
    for i, raw in rows:
        d, flat = json.loads(raw), {}
        for k in ("arousal", "valence"):
            if k in d:
                flat[k] = min(9.0, max(1.0, d[k]))  # the regression head can overshoot its 1..9 range
        flat.update({f"mood_{k}": v for k, v in d.get("mood", {}).items()})
        flat.update({f"timbre_{k}": v for k, v in d.get("timbre", {}).items()})
        if flat:
            out[i] = flat
    return out
