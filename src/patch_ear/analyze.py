"""Offline batch analysis: walk configured sources, measure, write the catalogue. Incremental by mtime+size+name.

Progress is reported through an `on_event(kind, **info)` callback so the CLI (tqdm) and the web UI share one code path.
"""

from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

import numpy as np
import soundfile as sf

from . import catalogue
from .config import Config, SourceConfig
from .features import EXTRA, FEATURES, extract, load, measured_midi
from .items import AVERAGE, REPRESENTATIVE, SINGLE, Item, Measured, bento_category, pick_at_pitch, spread

Event = Callable[..., None]

# "<patch>-<midi>_<note>" e.g. carnage_amp-048_c2 / hollow_tube-a-036_c1
_NOTE_SUFFIX = re.compile(r"^(?P<base>.+?)-(?P<midi>\d{2,3})_[a-g]#?-?\d+$", re.IGNORECASE)
_MIDI_ANYWHERE = re.compile(r"(?<!\d)(\d{2,3})_[a-g]#?-?\d+(?!\d)", re.IGNORECASE)


def _wavs(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.suffix.lower() == ".wav" and not p.name.startswith("."))


def _pick(files: list[Path], ref_midi: int) -> Path:
    """Sample nearest ref_midi if file names carry MIDI numbers, else the middle file by name."""
    scored = []
    for p in files:
        m = _MIDI_ANYWHERE.search(p.stem)
        if m:
            scored.append((abs(int(m[1]) - ref_midi), p.name, p))
    return min(scored)[2] if scored else files[len(files) // 2]


def _common_prefix_len(names: list[str]) -> int:
    """Length of the prefix shared by all names, cut back to a '-' boundary (e.g. 'wa-essence-bass_oneshots-')."""
    if len(names) < 2:
        return 0
    pre = os.path.commonprefix(names)
    i = pre.rfind("-")
    return i + 1 if i > 0 else 0


# ---------------- Bento patch.xml folders ----------------

def _norm_bento(path: str) -> str:
    p = path.strip().replace("\\", "/")
    return p.removeprefix("UserPatches/")


def read_patchindex(path: Path) -> dict[str, list[str]]:
    if not path.is_file():
        return {}
    root = ET.parse(path).getroot()
    return {_norm_bento(e.get("path", "")): [t.text for t in e.findall("tag") if t.text] for e in root.findall("patch")}


def read_list(path: Path) -> set[str]:
    return {_norm_bento(line) for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")}


def _bento_rep(patch: Path, wavs: list[Path], ref_midi: int, max_seconds: float = 3.0) -> Path:
    """Sample at the reference pitch (see items.pick_at_pitch); labels come from the rootnote in patch.xml."""
    try:
        roots = {}
        for p in ET.parse(patch / "patch.xml").getroot().iter("params"):
            if p.get("filename") and p.get("rootnote"):
                roots[p.get("filename")] = int(p.get("rootnote"))
        known = [(roots[w.name], w) for w in wavs if w.name in roots]
        if not known:
            return wavs[len(wavs) // 2]

        def probe(path: Path) -> float | None:
            chans, sr, _ = load(path, max_seconds)
            return measured_midi(chans, sr)

        return pick_at_pitch(known, ref_midi, probe)
    except (ET.ParseError, ValueError):
        return wavs[len(wavs) // 2]


def _bento_items(src: SourceConfig) -> list[Item]:
    root = src.path
    tags = read_patchindex(root / "patchindex.xml")
    only = read_list(src.only_list) if src.only_list else None
    if src.only_list and not src.only_list.is_file():
        raise FileNotFoundError(f"only_list not found: {src.only_list}")
    out = []
    for area in sorted(d for d in root.iterdir() if d.is_dir()):
        for patch in sorted(d for d in area.iterdir() if (d / "patch.xml").is_file()):
            rel = f"{area.name}/{patch.name}"
            if only is not None and rel not in only:
                continue
            wavs = sorted(w for w in patch.iterdir() if w.suffix.lower() == ".wav" and w.name.lower() != "preview.wav")
            if not wavs:
                continue
            t = tags.get(rel, [])
            kind = {"SampInst": "instrument", "OneShots": "kit", "Wavetable": "wavetable", "Granular": "granular"}.get(area.name, area.name.lower())
            # `type:` tags say what the audio file IS: time-based measures mean little for a single-cycle wavetable
            common = dict(id=f"{src.name}:{rel}", name=patch.name, n_samples=len(wavs), tags=[*t, f"type:{kind}"],
                          category=bento_category(t, area.name))
            if area.name == "OneShots":  # a kit: its character is the average over its hits
                out.append(Item(files=spread(wavs), mode=AVERAGE, **common))
            elif area.name == "SampInst":
                out.append(Item(files=[_bento_rep(patch, wavs, src.ref_midi)], mode=REPRESENTATIVE if len(wavs) > 1 else SINGLE, **common))
            else:  # Wavetable, Granular: one file
                out.append(Item(files=[wavs[0]], **common))
    return out


# ---------------- generic libraries ----------------

def items_for(src: SourceConfig) -> list[Item]:
    """Every catalogue row the source should produce."""
    if src.type == "bento":
        return _bento_items(src)
    root, wavs = src.path, _wavs(src.path)
    rel = lambda p: p.relative_to(root)  # noqa: E731

    if src.type in ("oneshots", "wavetable"):
        return [Item(f"{src.name}:{rel(p)}", p.stem, [p]) for p in wavs]

    if src.type in ("drumkit", "kit"):
        # drumkit: a folder directly holding hits = one kit. kit: each first-level subfolder = one kit (hits nested inside).
        kits: dict[Path, list[Path]] = {}
        for p in wavs:
            key = p.parent if src.type == "drumkit" else (root / rel(p).parts[0] if len(rel(p).parts) > 1 else root)
            kits.setdefault(key, []).append(p)
        return [Item(f"{src.name}:{rel(k)}", k.name if k != root else root.name, spread(files), AVERAGE, len(files))
                for k, files in sorted(kits.items())]

    if src.type == "filename":
        groups: dict[tuple[Path, str], list[Path]] = {}
        for p in wavs:
            m = _NOTE_SUFFIX.match(p.stem)
            groups.setdefault((p.parent, m["base"] if m else p.stem), []).append(p)
        cut = _common_prefix_len([b for _, b in groups])
        return [Item(f"{src.name}:{rel(folder) / base}", base[cut:] or base, [_pick(files, src.ref_midi)],
                     REPRESENTATIVE if len(files) > 1 else SINGLE, len(files))
                for (folder, base), files in sorted(groups.items())]

    # multisample: one folder (that directly holds WAVs) = one preset
    folders: dict[Path, list[Path]] = {}
    for p in wavs:
        folders.setdefault(p.parent, []).append(p)
    return [Item(f"{src.name}:{rel(f)}", f.name if str(rel(f)) != "." else root.name, [_pick(files, src.ref_midi)],
                 REPRESENTATIVE if len(files) > 1 else SINGLE, len(files))
            for f, files in sorted(folders.items())]


def plan_source(src: SourceConfig) -> tuple[list[Item], list[str]]:
    """(items, notes) for a source; notes are things worth telling the user (e.g. selected presets with no config)."""
    if src.type == "deluge":
        from . import press

        return press.plan(src)
    return items_for(src), []


def skip_reason(src: SourceConfig) -> str | None:
    if not src.stype.analyzable:
        return f"type '{src.type}' cannot be analysed yet"
    if not src.path.is_dir():
        return f"path not found: {src.path}"
    return None


def measure(files: list[Path], max_seconds: float) -> dict[str, float]:
    """Features of one file, or the mean over several (kits). Files that fail to load are skipped if others succeed."""
    results, last_err = [], None
    for f in files:
        try:
            chans, sr, dur = load(f, max_seconds)
            results.append(extract(chans, sr, dur))
        except Exception as e:
            last_err = e
    if not results:
        raise last_err or ValueError("no audio")
    return {k: float(np.mean([r[k] for r in results])) for k in FEATURES + EXTRA}


def _signature(item: Item) -> tuple[float, int, str]:
    stats = [f.stat() for f in (item.files or item.sig_files)]
    return (max(s.st_mtime for s in stats), sum(s.st_size for s in stats), item.name)


def _preview(cfg: Config, item: Item, m: Measured) -> Path:
    """Audition file for a preset that has no single file to point at (a plugin render, a mapped multisample)."""
    d = cfg.catalogue.parent / "previews"
    d.mkdir(parents=True, exist_ok=True)
    path = d / (hashlib.sha1(item.id.encode()).hexdigest()[:16] + ".wav")
    sf.write(str(path), m.chans[:, : int(8 * m.sr)].T, m.sr, subtype="PCM_16")
    return path


def analyze(cfg: Config, only: str | None = None, force: bool = False, on_event: Event | None = None,
            cancelled: Callable[[], bool] = lambda: False, limit: int | None = None) -> None:
    emit = on_event or (lambda *a, **k: None)
    conn = catalogue.connect(cfg.catalogue)
    have = {} if force else catalogue.known(conn)

    plans: list[tuple[SourceConfig, list[Item]]] = []
    for src in cfg.sources:
        if only and src.name != only:
            continue
        reason = skip_reason(src)
        if reason:
            emit("source_skip", source=src.name, reason=reason)
            continue
        try:
            items, notes = plan_source(src)
        except (OSError, ET.ParseError, RuntimeError) as e:
            emit("source_skip", source=src.name, reason=str(e))
            continue
        for n in notes:
            emit("source_note", source=src.name, note=n)
        plans.append((src, items[:limit] if limit else items))
    emit("plan", total=sum(len(i) for _, i in plans), sources={s.name: len(i) for s, i in plans})

    for src, items in plans:
        emit("source_start", source=src.name, total=len(items))
        counts = {"analysed": 0, "unchanged": 0, "failed": 0}
        for item in items:
            if cancelled():
                conn.commit()
                conn.close()
                emit("cancelled")
                return
            sig = _signature(item)
            lib = item.library or src.name
            if have.get(item.id) == sig:
                counts["unchanged"] += 1
                conn.execute("UPDATE patches SET library=? WHERE id=? AND library!=?", (lib, item.id, lib))  # backfill without re-measuring
                emit("item", source=src.name, name=item.name, state="unchanged")
                continue
            try:
                if item.capture:
                    m = item.capture(cfg.max_seconds)
                    feats, path, mode, n_samples = m.features, _preview(cfg, item, m), m.mode, m.n_samples
                else:
                    feats, path, mode, n_samples = measure(item.files, cfg.max_seconds), item.files[0], item.mode, item.n_samples
            except Exception as e:  # one bad file must not stop the run
                counts["failed"] += 1
                emit("item", source=src.name, name=item.name, state="failed", message=f"{item.name}: {type(e).__name__}: {e}")
                continue
            catalogue.upsert(conn, dict(
                id=item.id, name=item.name, source=src.name, source_kind=src.type, category=item.category or src.category,
                path=str(path), representative=mode, mtime=sig[0], size=sig[1], features=feats,
                tags=item.tags, n_samples=n_samples, library=lib,
            ))
            counts["analysed"] += 1
            if counts["analysed"] % 50 == 0:
                conn.commit()
            emit("item", source=src.name, name=item.name, state="analysed")
        removed = catalogue.prune(conn, src.name, {i.id for i in items})
        conn.commit()
        emit("source_end", source=src.name, removed=removed, **counts)
    conn.close()
    emit("finished")
