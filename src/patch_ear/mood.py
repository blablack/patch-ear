"""Mood and use-case tags from the CLAP embeddings (EXPERIMENTAL, zero-shot). Needs `uv sync --extra clap` to compute; querying needs nothing.

Every embedded sound is scored against a fixed vocabulary of text prompts. Scores are z-scored WITHIN the sound's category (an
"aggressive" kick means aggressive among kicks), and a tag is kept when it stands out (z >= TAG_Z), at most MAX_TAGS per group.
Stored in the `moods` table next to the catalogue; the MCP server only reads it. Like all CLAP use here it hears broad character,
not fine detail, and has not been checked against the user's ears except on bass: it proposes, the user's ears decide.
"""

from __future__ import annotations

import json
import sqlite3

import numpy as np

from . import embed

TAG_Z = 1.0
MAX_TAGS = 4
MIN_GROUP = 10  # fewer sounds than this in a category: rank against the whole catalogue instead

# tag -> text prompt. Mood describes the sound; use says where it would work in my music (post-rock / industrial / cinematic).
MOODS: dict[str, str] = {
    "aggressive": "an aggressive, harsh, angry sound",
    "calm": "a calm, peaceful, gentle sound",
    "dark": "a dark, ominous, brooding sound",
    "bright": "a bright, shiny, uplifting sound",
    "warm": "a warm, soft, rounded analog sound",
    "cold": "a cold, sterile, digital sound",
    "tense": "a tense, anxious, suspenseful sound",
    "dreamy": "a dreamy, floating, hazy sound",
    "melancholic": "a sad, melancholic, wistful sound",
    "epic": "a huge, epic, cinematic sound",
    "minimal": "a sparse, minimal, simple sound",
    "hypnotic": "a hypnotic, repetitive, trance-like sound",
    "industrial": "a harsh industrial, metallic, mechanical sound",
    "organic": "a natural, organic, acoustic sound",
    "eerie": "an eerie, unsettling, strange sound",
    "gritty": "a gritty, dirty, distorted, lo-fi sound",
    "clean": "a clean, pristine, polished sound",
    "heavy": "a heavy, massive, crushing sound",
    "delicate": "a delicate, fragile, tender sound",
    "driving": "a driving, energetic, propulsive sound",
    "glacial": "a slow, glacial, evolving sound",
    "vintage": "a vintage, retro, nostalgic sound",
    "futuristic": "a futuristic, sci-fi, synthetic sound",
    "chaotic": "a chaotic, noisy, unpredictable sound",
}
USES: dict[str, str] = {
    "quiet-intro": "a quiet, sparse intro that slowly begins a piece of music",
    "tension-build": "a rising, tense build-up before a climax",
    "heavy-climax": "a huge, loud, powerful climax with full distortion",
    "outro-fade": "a gentle, fading ending that dissolves into silence",
    "ambient-bed": "a soft, constant ambient bed that sits under other instruments",
    "main-pulse": "a steady, hypnotic rhythmic pulse that carries a whole track",
    "city-night": "the sound of a lonely city at night, neon, rain and distant traffic",
    "industrial-machinery": "a factory, machinery and industrial noise texture",
    "cinematic-score": "dramatic film-score music",
    "transition": "a transition, riser, swell or sweep between two sections",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS moods (
    id TEXT PRIMARY KEY,   -- patches.id
    mood TEXT NOT NULL,    -- JSON list of tags that stand out for this sound, strongest first
    use TEXT NOT NULL,     -- JSON list of use-case tags
    scores TEXT NOT NULL   -- JSON {tag: z-score within the category}, every tag
);
"""


def compute(conn: sqlite3.Connection, log=print) -> int:
    """(Re)score every embedded sound. Needs the CLAP text model (loaded once)."""
    conn.executescript(SCHEMA)
    ids, mat = embed.load(conn)
    if not ids:
        log("nothing embedded yet: run `patch-ear embed` first")
        return 0
    tags = [*MOODS, *USES]
    text = embed.embed_text([*MOODS.values(), *USES.values()])
    sims = mat @ text.T  # (n, tags)
    cat = {r[0]: r[1] for r in conn.execute("SELECT id, category FROM patches")}
    cats = np.array([cat.get(i, "") for i in ids])
    z = np.zeros_like(sims)
    for c in set(cats):
        m = cats == c
        ref = sims[m] if m.sum() >= MIN_GROUP else sims
        z[m] = (sims[m] - ref.mean(axis=0)) / (ref.std(axis=0) + 1e-9)
    n_mood = len(MOODS)
    for k, i in enumerate(ids):
        def top(lo: int, hi: int) -> list[str]:
            order = np.argsort(-z[k, lo:hi])[:MAX_TAGS]
            return [tags[lo + j] for j in order if z[k, lo + j] >= TAG_Z]

        conn.execute("INSERT OR REPLACE INTO moods VALUES (?,?,?,?)",
                     (i, json.dumps(top(0, n_mood)), json.dumps(top(n_mood, len(tags))),
                      json.dumps({t: round(float(z[k, j]), 2) for j, t in enumerate(tags)})))
    conn.commit()
    log(f"{len(ids)} sounds tagged")
    return len(ids)


def load(conn: sqlite3.Connection) -> dict[str, dict]:
    """{patch id: {"mood": [...], "use": [...], "scores": {...}}}; empty if moods were never computed."""
    try:
        rows = conn.execute("SELECT id, mood, use, scores FROM moods").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r[0]: {"mood": json.loads(r[1]), "use": json.loads(r[2]), "scores": json.loads(r[3])} for r in rows}
