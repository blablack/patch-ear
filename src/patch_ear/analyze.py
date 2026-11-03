"""Offline batch analysis: walk configured sources, measure, write the catalogue. Incremental by mtime+size+name.

Progress is reported through an `on_event(kind, **info)` callback so the CLI (tqdm) and the web UI share one code path.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from pathlib import Path

import numpy as np
import soundfile as sf

from . import catalogue
from .config import Config, SourceConfig
from .features import EXTRA, FEATURES, extract, load, measured_midi
from .items import AVERAGE, REPRESENTATIVE, SINGLE, Item, Measured, spread

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


# ---------------- one library folder, shapes told apart per folder ----------------

_KIT_DIR = re.compile(r"\bkits?\b", re.IGNORECASE)
_NOTE_TOKEN = re.compile(r"(?<![A-Za-z])[A-G][#b]?-?\d(?!\d)")  # C#3, A2
_CATEGORY_WORDS = {"bass": "Bass", "lead": "Lead", "pad": "Pad", "pluck": "Pluck", "arp": "Arp", "keys": "Keys", "piano": "Keys",
                   "strings": "Strings", "brass": "Brass", "fx": "FX", "vocal": "Vocal", "choir": "Vocal"}


_NOTE_END = re.compile(r"^(?P<base>.+?)[_\- ](?P<note>[A-G][#b]?-?\d?)$")  # Calm_C2, Calm_D
_MIDI_END = re.compile(r"^(?P<base>.+?)[_\- ](?P<midi>\d{2,3})$")  # SFM-MicroMoog1-24


def _split_note(stem: str) -> tuple[str, str] | None:
    """(preset base, note) when the name ends in a note: '<patch>-048_c2', '<patch>_C#2', '<patch>-24' (21..108)."""
    for pat, key in ((_NOTE_SUFFIX, "midi"), (_NOTE_END, "note"), (_MIDI_END, "midi")):
        m = pat.match(stem)
        if m and (pat is not _MIDI_END or 21 <= int(m["midi"]) <= 108):
            return m["base"], m[key]
    return None


def _is_multisample(files: list[Path]) -> bool:
    """Several files whose names carry a note (see _split_note), or contain one (048_c2, C#3) somewhere."""
    if len(files) < 3:
        return False
    parsed = [_split_note(p.stem) for p in files]
    if sum(1 for x in parsed if x) / len(files) >= 0.6 and len({x[1] for x in parsed if x}) >= 3:
        return True
    return sum(1 for p in files if _MIDI_ANYWHERE.search(p.stem) or _NOTE_TOKEN.search(p.stem)) / len(files) >= 0.6


def _guess_category(parts: tuple[str, ...], fallback: str) -> str:
    for word in re.split(r"[^a-z]+", " ".join(parts).lower()):
        if word in _CATEGORY_WORDS:
            return _CATEGORY_WORDS[word]
    return fallback


def _library_items(src: SourceConfig) -> list[Item]:
    """Kits, multisamples and one-shots found under one folder. The shape is decided per folder:
    - anything under a folder called "Kits" (or "... Kit"): the folder just below it is one kit (hits nested inside);
    - a folder whose file names carry notes: one multisample preset;
    - otherwise every file is a one-shot.
    Point the source at the folders you want; nothing here filters out drum hits or loops."""
    root, fallback = src.path, src.category if src.category != "auto" else "Synth"
    kits: dict[Path, list[Path]] = {}
    folders: dict[Path, list[Path]] = {}
    base = root.parent if _KIT_DIR.search(root.name) else root  # a source pointed AT the "Kits" folder
    for p in _wavs(root):
        parts = p.relative_to(base).parent.parts
        i = next((i for i, part in enumerate(parts) if _KIT_DIR.search(part)), None)
        if i is not None:
            # the folder just below "Kits" is the kit, unless its children are themselves kits ("CR78/01. Kit 1")
            depth = i + 3 if len(parts) > i + 2 and _KIT_DIR.search(parts[i + 2]) else min(i + 2, len(parts))
            kits.setdefault(base.joinpath(*parts[:depth]), []).append(p)
        else:
            folders.setdefault(p.parent, []).append(p)
    out: list[Item] = []
    kit_names = [k.name for k in kits]
    for k, files in sorted(kits.items()):
        rel = k.relative_to(root.parent if _KIT_DIR.search(root.name) else root)
        name = k.name if kit_names.count(k.name) == 1 else f"{k.parent.name} / {k.name}"
        out.append(Item(f"{src.name}:{rel}", name, spread(files), AVERAGE, len(files), category="Drum", tags=["type:kit"]))
    for f, files in sorted(folders.items()):
        rel = f.relative_to(root)
        if _is_multisample(files):
            groups: dict[str, list[Path]] = {}  # several presets in one folder, told apart by "<patch>-<midi>_<note>" names
            for p in files:
                parsed = _split_note(p.stem)
                groups.setdefault(parsed[0] if parsed else p.stem, []).append(p)
            cat = _guess_category(rel.parts, fallback)
            if len(groups) > 1 and any(len(g) > 1 for g in groups.values()):
                cut = _common_prefix_len(list(groups))
                for base, g in sorted(groups.items()):
                    out.append(Item(f"{src.name}:{rel / base}", base[cut:] or base, [_pick(g, src.ref_midi)],
                                    REPRESENTATIVE if len(g) > 1 else SINGLE, len(g), category=cat, tags=["type:instrument"]))
            else:
                out.append(Item(f"{src.name}:{rel}", f.name if str(rel) != "." else root.name, [_pick(files, src.ref_midi)],
                                REPRESENTATIVE, len(files), category=cat, tags=["type:instrument"]))
        else:
            for p in files:
                out.append(Item(f"{src.name}:{p.relative_to(root)}", p.stem, [p], category=_guess_category(rel.parts, fallback), tags=["type:oneshot"]))
    return out


# ---------------- generic libraries ----------------

def items_for(src: SourceConfig) -> list[Item]:
    """Every catalogue row the source should produce."""
    if src.type == "library":
        return _library_items(src)
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
    if src.type == "press":
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
        except (OSError, RuntimeError) as e:
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
            lib = item.library or src.library or src.path.name
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
