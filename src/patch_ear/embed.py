"""CLAP audio-text embeddings (EXPERIMENTAL). Optional: needs `uv sync --extra clap`.

Each preset's audition file (the file the catalogue points at: the reference sample, a kit preview, a rendered preview) is embedded
into the shared audio-text space of LAION-CLAP, so a phrase ("warm analog pad") can be matched against sound. Stored next to the
catalogue in an `embeddings` table; the analyzer and the MCP server do not depend on it.

`probe` is the sanity check: it scores every preset on opposite text pairs ("a heavy deep sub bass" vs "a thin bright bass") and
reports the rank correlation with the hand-built features that were validated against the user's ears. It needs no listening.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np

MODEL = "laion/clap-htsat-unfused"
SR = 48000
SECONDS = 10.0  # the audio encoder takes 10 s windows

SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    id TEXT PRIMARY KEY,   -- patches.id
    model TEXT NOT NULL,
    path TEXT NOT NULL,    -- the file that was embedded (stale if the catalogue now points elsewhere)
    vec BLOB NOT NULL      -- float32, L2-normalised
);
"""

# Zero-shot axes: (feature or score, positive text, negative text). The feature is what the user's ears already validated.
PROBES = [
    ("weight", "a heavy, deep, sub bass sound", "a thin, bright, light bass sound"),
    ("grit", "a gritty, distorted, harsh sound", "a clean, smooth, pure sound"),
    ("roughness", "a rough, buzzing, growling bass", "a smooth, steady, calm bass"),
    ("inharmonic_ratio", "a metallic, bell-like, clangorous sound", "a clean, warm, harmonic tone"),
    ("decay_s", "a long, sustained, ringing sound", "a short, plucky, percussive sound"),
]

_cache: dict = {}


def _load():
    if "m" not in _cache:
        try:
            import torch
            from transformers import ClapModel, ClapProcessor
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("text search needs the optional extra: uv sync --extra clap") from e
        torch.set_num_threads(max(1, (__import__("os").cpu_count() or 4) - 1))
        _cache["m"] = (ClapModel.from_pretrained(MODEL).eval(), ClapProcessor.from_pretrained(MODEL), torch)
    return _cache["m"]


def _vec(out) -> "np.ndarray":
    """Embedding tensor from whatever this transformers version returns (a tensor, or an output object)."""
    for attr in ("pooler_output", "audio_embeds", "text_embeds"):
        if hasattr(out, attr) and getattr(out, attr) is not None:
            out = getattr(out, attr)
            break
    v = out.detach().cpu().numpy().astype("float32")
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)


def embed_audio(paths: list[Path]) -> np.ndarray:
    import librosa

    model, proc, torch = _load()
    waves = [librosa.load(str(p), sr=SR, mono=True, duration=SECONDS)[0] for p in paths]
    try:
        inputs = proc(audio=waves, sampling_rate=SR, return_tensors="pt")
    except TypeError:  # older transformers call it `audios`
        inputs = proc(audios=waves, sampling_rate=SR, return_tensors="pt")
    with torch.no_grad():
        return _vec(model.get_audio_features(**inputs))


def embed_text(texts: list[str]) -> np.ndarray:
    model, proc, torch = _load()
    inputs = proc(text=texts, return_tensors="pt", padding=True)
    with torch.no_grad():
        return _vec(model.get_text_features(**inputs))


def run(conn: sqlite3.Connection, category: str | None = None, limit: int | None = None, batch: int = 8, force: bool = False,
        log=print) -> int:
    """Embed the catalogue's audition files (optionally one category). Skips rows already embedded from the same file."""
    conn.executescript(SCHEMA)
    have = {} if force else {r[0]: r[1] for r in conn.execute("SELECT id, path FROM embeddings WHERE model=?", (MODEL,))}
    q, args = "SELECT id, path FROM patches", ()
    if category:
        q, args = q + " WHERE category=?", (category,)
    todo = [(i, p) for i, p in conn.execute(q, args) if have.get(i) != p and Path(p).is_file()]
    if limit:
        todo = todo[:limit]
    log(f"{len(todo)} to embed")
    done = 0
    for k in range(0, len(todo), batch):
        chunk = todo[k:k + batch]
        try:
            vecs = embed_audio([Path(p) for _, p in chunk])
        except Exception:  # one unreadable file must not stop the run: retry one by one
            ok = []
            for c in chunk:
                try:
                    ok.append((c, embed_audio([Path(c[1])])[0]))
                except Exception as e2:
                    log(f"  skipped {c[0]}: {type(e2).__name__}: {e2}")
            chunk, vecs = [c for c, _ in ok], [v for _, v in ok]
        for (i, p), v in zip(chunk, vecs):
            conn.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?)", (i, MODEL, p, np.asarray(v, dtype="float32").tobytes()))
            done += 1
        conn.commit()
        log(f"  {min(k + batch, len(todo))}/{len(todo)}")
    return done


def load(conn: sqlite3.Connection, category: str | None = None) -> tuple[list[str], np.ndarray]:
    q, args = "SELECT e.id, e.vec FROM embeddings e JOIN patches p ON p.id=e.id WHERE e.model=?", [MODEL]
    if category:
        q, args = q + " AND p.category=?", [*args, category]
    rows = conn.execute(q, args).fetchall()
    return [r[0] for r in rows], (np.stack([np.frombuffer(r[1], dtype="float32") for r in rows]) if rows else np.zeros((0, 512), "float32"))


def search(conn: sqlite3.Connection, text: str, category: str | None = None, n: int = 10) -> list[tuple[str, float]]:
    ids, mat = load(conn, category)
    if not ids:
        return []
    sims = mat @ embed_text([text])[0]
    return [(ids[i], float(sims[i])) for i in np.argsort(-sims)[:n]]


def neighbours(conn: sqlite3.Connection, patch_id: str, n: int = 10, category: str | None = None) -> list[tuple[str, float]]:
    """Presets whose stored vector is closest to this preset's. Pure numpy: no model, safe for the MCP server."""
    ids, mat = load(conn, category)
    if patch_id not in ids:
        raise ValueError(f"'{patch_id}' has no embedding yet (run `patch-ear embed`)")
    sims = mat @ mat[ids.index(patch_id)]
    return [(ids[i], float(sims[i])) for i in np.argsort(-sims) if ids[i] != patch_id][:n]


def probe(conn: sqlite3.Connection, category: str | None = None) -> list[dict]:
    """Rank correlation (Spearman) between each zero-shot text axis and the feature it should track."""
    from scipy.stats import spearmanr

    from . import catalogue

    ids, mat = load(conn, category)
    if len(ids) < 10:
        raise SystemExit(f"only {len(ids)} embedded presets; run `patch-ear embed` first")
    ranked = {r["id"]: r for r in catalogue.load_ranked(conn, category)}
    out = []
    for feat, pos, neg in PROBES:
        t = embed_text([pos, neg])
        score = mat @ t[0] - mat @ t[1]
        ref = [ranked[i]["pct"][feat] if feat in ("grit", "weight") else ranked[i]["features"][feat] for i in ids]
        rho, p = spearmanr(score, ref)
        out.append({"feature": feat, "positive": pos, "negative": neg, "spearman": float(rho), "n": len(ids), "p": float(p)})
    return out
