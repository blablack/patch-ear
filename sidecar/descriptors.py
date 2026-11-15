"""Trained mood scores (Essentia) and perceptual timbre scores (AudioCommons timbral_models) for every catalogue sound.

OFFLINE SIDECAR. Runs in its own Python 3.12 environment (essentia-tensorflow has no 3.14 wheels), NOT in the patch-ear
project environment: `just sidecar-setup` builds it, `just descriptors` runs it. It only READS patches.path from the catalogue
and WRITES the `descriptors` table; the MCP server (3.14, read-only) just reads that table.

Per sound (the catalogue's audition file, first ~20 s):
  - Essentia Discogs-EffNet heads: mood_aggressive, mood_relaxed, mood_sad, mood_happy, mood_party, mood_acoustic, mood_electronic,
    danceability, instrumental (voice_instrumental), tonal (tonal_atonal): probabilities 0..1 of the FIRST class, frames averaged.
  - Essentia MSD-MusiCNN emomusic head: valence and arousal (the model's native 1..9 scale).
  - timbral_models: hardness, depth, brightness, roughness, warmth, sharpness, booming, reverb (0..100, can exceed).
The mood models were trained on listener-labelled MUSIC and are only moderately reliable (annotator agreement was moderate); the timbral
models were trained on sound effects. Neither is ear-validated here: they propose, the user's ears decide.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

HOME = Path(os.environ.get("PATCH_EAR_HOME", Path(__file__).resolve().parent.parent / "data"))
MODELS = HOME / "essentia-models"
VERSION = 1
SECONDS = 20.0  # mood models: first 20 s
TIMBRE_SECONDS = 8.0  # timbre models are slow, and the catalogue measures the first 8 s everywhere
NO_TIMBRE = {"Drum"}  # whole-kit previews (concatenated hits): a timbre score for that is meaningless
PRIORITY = ["Kick", "Snare", "Hat", "Tom", "Clap", "Cymbal", "Bass", "Loop", "Pad", "Lead", "Atmosphere", "FX", "Keys", "Synth"]  # then the rest

HEADS = {  # name -> (model file stem, index of the positive class)
    "aggressive": ("mood_aggressive-discogs-effnet-1", 0), "relaxed": ("mood_relaxed-discogs-effnet-1", 0),
    "sad": ("mood_sad-discogs-effnet-1", 0), "happy": ("mood_happy-discogs-effnet-1", 0),
    "party": ("mood_party-discogs-effnet-1", 0), "acoustic": ("mood_acoustic-discogs-effnet-1", 0),
    "electronic": ("mood_electronic-discogs-effnet-1", 0), "danceable": ("danceability-discogs-effnet-1", 0),
    "instrumental": ("voice_instrumental-discogs-effnet-1", 0), "tonal": ("tonal_atonal-discogs-effnet-1", 1),
}  # classes per the model .json files: voice_instrumental = [instrumental, voice]; tonal_atonal = [atonal, tonal]

SCHEMA = """
CREATE TABLE IF NOT EXISTS descriptors (
    id TEXT PRIMARY KEY,   -- patches.id
    path TEXT NOT NULL,    -- the audition file that was analysed (stale if the catalogue now points elsewhere)
    version INTEGER NOT NULL,
    data TEXT NOT NULL     -- JSON {"mood": {...}, "valence": x, "arousal": y, "timbre": {...}}
);
"""


class Models:
    def __init__(self, mood: bool, timbre: bool):
        self.mood, self.timbre = mood, timbre
        if mood:
            import essentia.standard as es

            self.es = es
            self.effnet = es.TensorflowPredictEffnetDiscogs(graphFilename=str(MODELS / "discogs-effnet-bs64-1.pb"), output="PartitionedCall:1")
            self.heads = {n: es.TensorflowPredict2D(graphFilename=str(MODELS / f"{stem}.pb"), output="model/Softmax") for n, (stem, _) in HEADS.items()}
            self.musicnn = es.TensorflowPredictMusiCNN(graphFilename=str(MODELS / "msd-musicnn-1.pb"), output="model/dense/BiasAdd")
            self.emo = es.TensorflowPredict2D(graphFilename=str(MODELS / "emomusic-msd-musicnn-2.pb"), output="model/Identity")
        if timbre:
            np.lib.pad = np.pad  # timbral_models still calls numpy.lib.pad, removed in numpy 2
            import librosa
            import librosa.core
            import librosa.onset

            # timbral_models calls these positionally; librosa >= 0.10 made their arguments keyword-only.
            _strength, _detect, _resample = librosa.onset.onset_strength, librosa.onset.onset_detect, librosa.core.resample
            librosa.onset.onset_strength = lambda y=None, sr=22050, **kw: _strength(y=y, sr=sr, **kw)
            librosa.onset.onset_detect = lambda y=None, sr=22050, **kw: _detect(y=y, sr=sr, **kw)
            librosa.core.resample = librosa.resample = lambda y, orig_sr, target_sr, **kw: _resample(y, orig_sr=orig_sr, target_sr=target_sr, **kw)
            import timbral_models

            self.tm = timbral_models

    def analyse(self, path: str, category: str = "") -> dict:
        out: dict = {}
        if self.mood:
            es = self.es
            audio16 = es.MonoLoader(filename=path, sampleRate=16000, resampleQuality=4)()
            audio16 = audio16[: int(SECONDS * 16000)]
            if len(audio16) < 16000 * 3:  # the models want a few seconds: pad short hits with silence
                audio16 = np.pad(audio16, (0, 16000 * 3 - len(audio16)))
            emb = self.effnet(audio16)
            out["mood"] = {n: round(float(np.mean(h(emb)[:, HEADS[n][1]])), 3) for n, h in self.heads.items()}
            va = np.mean(self.emo(self.musicnn(audio16)), axis=0)  # classes: [valence, arousal]
            out["valence"], out["arousal"] = round(float(va[0]), 3), round(float(va[1]), 3)
        if self.timbre and category not in NO_TIMBRE:
            try:
                import soundfile as sf

                audio, fs = sf.read(path, frames=int(TIMBRE_SECONDS * sf.info(path).samplerate), always_2d=False)
                t = self.tm.timbral_extractor(audio, fs=fs, dev_output=False, phase_correction=False, clip_output=True)
                out["timbre"] = {k: round(float(v), 1) for k, v in t.items()}
            except Exception as e:  # a failing timbre model must not lose the mood scores
                out["timbre_error"] = f"{type(e).__name__}: {e}"[:200]
        return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=str(HOME / "catalogue.db"))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-timbre", action="store_true")
    ap.add_argument("--no-mood", action="store_true")
    ap.add_argument("--category", help="only this category (e.g. Kick, Loop, Bass)")
    ap.add_argument("--ids", nargs="*", help="only these patch ids")
    ap.add_argument("--shard", help="K/N: take every N-th sound starting at K, so N processes can share the work (e.g. 0/3)")
    a = ap.parse_args()
    conn = sqlite3.connect(a.db, timeout=300)
    conn.executescript(SCHEMA)
    have = {} if a.force else {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, path, version FROM descriptors")}
    q, args = "SELECT id, path, category FROM patches", []
    if a.category:
        q, args = q + " WHERE category=?", [a.category]
    rank = {c: k for k, c in enumerate(PRIORITY)}
    rows = sorted(conn.execute(q, args), key=lambda r: rank.get(r[2], len(PRIORITY)))
    todo = [(i, p, c) for i, p, c in rows if have.get(i) != (p, VERSION) and Path(p).is_file() and (not a.ids or i in a.ids)]
    if a.shard:
        k, n_sh = (int(x) for x in a.shard.split("/"))
        todo = todo[k::n_sh]
    if a.limit:
        todo = todo[: a.limit]
    print(f"{len(todo)} to analyse", flush=True)
    m = Models(mood=not a.no_mood, timbre=not a.no_timbre)
    t0, failed = time.time(), 0
    for n, (i, p, c) in enumerate(todo, 1):
        try:
            data = m.analyse(p, c)
        except Exception as e:
            failed += 1
            print(f"  skipped {i}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            continue
        conn.execute("INSERT OR REPLACE INTO descriptors VALUES (?,?,?,?)", (i, p, VERSION, json.dumps(data)))
        conn.commit()  # short write transactions: several shards share this db
        if n % 25 == 0:
            rate = n / (time.time() - t0)
            print(f"  {n}/{len(todo)}  {rate:.2f}/s  eta {int((len(todo) - n) / max(rate, 1e-9) / 60)} min", flush=True)
    conn.commit()
    print(f"done: {len(todo) - failed} analysed, {failed} failed", flush=True)


if __name__ == "__main__":
    main()
