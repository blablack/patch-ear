"""Offline batch analysis: walk configured sources, measure, write the catalogue. Incremental by mtime+size+name.

Sources name their ORIGINS (NAS folders, zips, folders inside zips, or a union of those via mounts; see origin.py).
Walking and change detection read metadata only; a sound's audio is fetched through the shared patch-press cache
only when it is measured, and the catalogue stores the origin, not the local copy.

Progress is reported through an `on_event(kind, **info)` callback so the CLI (tqdm) and the web UI share one code path.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from collections.abc import Callable
from pathlib import Path

import numpy as np
import soundfile as sf

from . import catalogue, origin
from .config import Config, SourceConfig
from .features import EXTRA, FEATURES, extract, load, measured_midi
from .items import AVERAGE, NOTES, REPRESENTATIVE, SINGLE, Item, Measured, spread

Event = Callable[..., None]

# "<patch>-<midi>_<note>" e.g. carnage_amp-048_c2 / hollow_tube-a-036_c1
_NOTE_SUFFIX = re.compile(r"^(?P<base>.+?)-(?P<midi>\d{2,3})_[a-g]#?-?\d+$", re.IGNORECASE)
_MIDI_ANYWHERE = re.compile(r"(?<![A-Za-z0-9])(\d{2,3})_[a-g]#?-?\d+(?!\d)", re.IGNORECASE)  # not "SH101_E6"


def _excluded(rel: Path, patterns: list[str]) -> bool:
    """A file is left out when a pattern (fnmatch, case-insensitive) matches one of its folder names, a leading path of it, or the
    file's own name (with or without .wav), so `Bongo*` drops loose files from a flat folder."""
    parts = rel.parent.parts
    heads = ["/".join(parts[: i + 1]).lower() for i in range(len(parts))]
    names = (rel.name.lower(), rel.stem.lower())
    return any(fnmatch.fnmatch(x, pat.lower()) for pat in patterns for x in (*heads, *(p.lower() for p in parts), *names))


def _wavs(src: SourceConfig) -> list[Path]:
    """The source's WAVs as paths under src.path (origin.list_files: a folder walk or a zip listing, no audio read)."""
    root, exclude = src.path, src.exclude
    return sorted(p for p in origin.list_files(src) if p.suffix.lower() == ".wav" and not p.name.startswith(".")
                  and not (exclude and _excluded(p.relative_to(root), exclude)))


def _note_midi(stem: str) -> int | None:
    """MIDI number from a file name: a number like '048_c2' / '-24', else a note name like 'D#7' (C3 = 60). None if there is none."""
    m = _MIDI_ANYWHERE.search(stem)
    if m:
        return int(m[1])
    parsed = _split_note(stem)
    if parsed:
        note = parsed[1]
        lead, n = _LEADING_NUM.match(stem), re.match(r"^([A-G])([#b]?)(-?\d)$", note)
        if lead and n and int(lead[1]) < 128:  # '57_ElectricBass_A2': the number is the key that was sent, whatever octave the label uses
            pc = (_PITCH_CLASS[n[1]] + (1 if n[2] == "#" else -1 if n[2] == "b" else 0)) % 12
            if int(lead[1]) % 12 == pc:
                return int(lead[1])
        if note.isdigit():
            return int(note)
        n = re.match(r"^([A-G])([#b]?)(-?\d)$", note)
        if n:
            return 12 * (int(n[3]) + 1) + _PITCH_CLASS[n[1]] + (1 if n[2] == "#" else -1 if n[2] == "b" else 0)
    return None


def _pick_notes(files: list[Path], ref_midi: int) -> tuple[list[Path], int]:
    """(files to measure, mode). Matched on the note in the NAME, i.e. the key that was sent, not the sounding pitch: a patch with its
    oscillators an octave up really does sound thin at that key. Up to three samples: the reference note first (it is the audition),
    then an octave below and above. Names without notes fall back to the middle file."""
    if len(files) == 1:
        return files, SINGLE
    picks: list[Path] = []
    for target in (ref_midi, ref_midi - 12, ref_midi + 12):
        near = [(abs(m - target), p.name, p) for p in files if (m := _note_midi(p.stem)) is not None]
        if near and (p := min(near)[2]) not in picks:
            picks.append(p)
    return (picks, NOTES) if len(picks) > 1 else ([picks[0] if picks else files[len(files) // 2]], REPRESENTATIVE)


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
_EXPORT_HASH = re.compile(r"-[A-Z0-9]{4}$")
_ROUND_ROBIN = re.compile(r"[_\- ]\d{3,4}$")  # trailing take counter: _0002
_MIDI_END = re.compile(r"^(?P<base>.+?)[_\- ](?P<midi>\d{2,3})$")  # SFM-MicroMoog1-24


_PITCH_CLASS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_LEADING_NUM = re.compile(r"^(\d{1,3})[_\- ]+(?=\S)")


def _drop_midi_prefix(base: str, note: str) -> str:
    """'100 Medium Muff OB' + 'E6' -> 'Medium Muff OB': a leading number that is the note's own MIDI number (either octave
    convention, give or take a semitone of off-by-one) is per-file, so left in it would make every note its own preset."""
    m, n = _LEADING_NUM.match(base), re.match(r"^([A-G])([#b]?)(-?\d)$", note)
    if not (m and n):
        return base
    midi = 12 * (int(n[3]) + 1) + _PITCH_CLASS[n[1]] + (1 if n[2] == "#" else -1 if n[2] == "b" else 0)
    return base[m.end():] if int(m[1]) in (midi - 13, midi - 12, midi - 11, midi - 1, midi, midi + 1, midi + 11, midi + 12, midi + 13) else base


def _split_note(stem: str) -> tuple[str, str] | None:
    """(preset base, note) when the name ends in a note: '<patch>-048_c2', '<patch>_C#2', '<patch>-24' (21..108)."""
    rr = _ROUND_ROBIN.sub("", stem)  # '<patch>_F#5_0002': a round-robin take of the same note belongs to the same preset
    for s in dict.fromkeys((stem, rr, _EXPORT_HASH.sub("", stem))):  # '<patch>_C#0-MU9H': exporter's random tag after the note
        for pat, key in ((_NOTE_SUFFIX, "midi"), (_NOTE_END, "note"), (_MIDI_END, "midi")):
            m = pat.match(s)
            if m and (pat is not _MIDI_END or 21 <= int(m["midi"]) <= 108):
                return (_drop_midi_prefix(m["base"], m[key]) if key == "note" else m["base"]), m[key]
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
    for p in _wavs(src):
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
                    picks, mode = _pick_notes(g, src.ref_midi)
                    out.append(Item(f"{src.name}:{rel / base}", base[cut:] or base, picks, mode, len(g), category=cat, tags=["type:instrument"]))
            else:
                picks, mode = _pick_notes(files, src.ref_midi)
                out.append(Item(f"{src.name}:{rel}", f.name if str(rel) != "." else root.name, picks, mode, len(files),
                                category=cat, tags=["type:instrument"]))
        else:
            for p in files:
                out.append(Item(f"{src.name}:{p.relative_to(root)}", p.stem, [p], category=_guess_category(rel.parts, fallback), tags=["type:oneshot"]))
    return out


# ---------------- drum machines, per role ----------------

# Checked in this order against the lower-cased word tokens of the path (after the source root) and the file name.
_ROLES: list[tuple[str, set[str]]] = [
    ("Kick", {"kick", "bd", "bassdrum", "bass", "kicks"}),
    ("Snare", {"snare", "sd", "snares", "snaredrum"}),
    ("Clap", {"clap", "claps", "handclap"}),
    ("Hat", {"hat", "hats", "hh", "ch", "oh", "hihat", "hihats"}),
    ("Tom", {"tom", "toms"}),
    ("Cymbal", {"cymbal", "crash", "ride", "china", "cymbals"}),
]

def _role_of(rel: Path) -> str | None:
    """The role of one hit (Kick, Snare, ...) from its file name, or None for percussion and anything else."""
    stem = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", rel.stem)  # camelCase: "HiHatClosed" -> "Hi Hat Closed", "FloorTom" -> "Floor Tom"
    toks = [re.sub(r"[^a-z]+", "", stem.lower()), *(t for t in re.split(r"[^a-z]+", stem.lower()) if t)]  # "Bass Drum" -> "bassdrum"
    words = set(toks)
    return next((r for r, kw in _ROLES if words & kw), None)


def _machine_items(src: SourceConfig) -> list[Item]:
    """PRE-MADE KITS only (never the individual-hit folders): point the source at a pack's Kits folder. Each folder that directly
    holds hits is one kit. Per kit: one whole-kit row (category Drum, averaged over its hits) plus one row per role present in it
    (Kick, Snare, ... category = role), so kits can be compared by their own kick/snare/hat."""
    root = src.path
    base = root.parent if re.search(r"\bkits?\b", root.name, re.IGNORECASE) else root  # keep ids like "02. Kits/<kit>"
    machine = src.library or root.name
    prefix = "" if machine.startswith(("essential", "vinyl")) else re.sub(r"[_-]+", " ", machine).strip().title().replace(" From Mars", "")
    kits: dict[Path, list[Path]] = {}
    for p in _wavs(src):
        kits.setdefault(p.parent, []).append(p)
    out: list[Item] = []
    for k, files in sorted(kits.items()):
        rel_parts = k.relative_to(root).parts
        if len(rel_parts) >= 2:
            name = " / ".join(rel_parts[-2:])
        else:
            name = f"{prefix} / {k.name}" if prefix and k != root else (k.name if k != root else root.name)
        rel = k.relative_to(base)
        out.append(Item(f"{src.name}:{rel}", name, spread(files), AVERAGE, len(files), category="Drum", tags=["type:kit"], library=machine))
        by_role: dict[str, list[Path]] = {}
        for p in files:
            r = _role_of(p.relative_to(k))
            if r:
                by_role.setdefault(r, []).append(p)
        for role, rf in sorted(by_role.items()):
            out.append(Item(f"{src.name}:{rel}:{role}", f"{name} / {role}", spread(rf), AVERAGE if len(rf) > 1 else SINGLE, len(rf),
                            category=role, tags=["type:machine-role", f"role:{role.lower()}", "kit"], library=machine))
    return out


# ---------------- loops ----------------

_LOOP_ROLES: list[tuple[str, tuple[str, ...]]] = [
    ("texture", ("texture", "backdrop", "fragments")), ("drone", ("drone", "ambient", "atmos")), ("bass", ("bass",)), ("melodic", ("melod", "chord", "lead", "pad", "arp")),
    ("guitar", ("guitar", "resonator", "pedal steel")),
    ("fx", ("fx", "sfx", "noise", "riser")), ("drums", ("beat", "drum", "groove", "kick", "snare", "hat", "tom", "perc", "top", "clap", "cymbal")),
    ("synth", ("synth", "acid", "organ", "vp330", "svc", "moog", "clavinet")),
]


def _loop_role(rel: Path, library: str = "") -> str:
    # The library name counts too: a pack called "... Bass" is bass even when its file names say "Groove".
    text = f"{'/'.join(rel.parts)} {library}".lower().replace("bass drum", "kick").replace("bassdrum", "kick")  # a bass-drum loop is drums
    return next((role for role, kws in _LOOP_ROLES if any(k in text for k in kws)), "loop")


def _loop_items(src: SourceConfig) -> list[Item]:
    out = []
    files = _wavs(src)
    stems: dict[str, int] = {}
    for p in files:
        stems[p.stem] = stems.get(p.stem, 0) + 1
    library = src.library or src.path.name
    for p in files:
        rel = p.relative_to(src.path)
        # A repeated file name (Dry/Amped, Clean/Wah treatments) gets its folder appended; the folders become tags to search by.
        name = f"{p.stem} ({p.parent.name})" if stems[p.stem] > 1 and p.parent != src.path else p.stem
        tags = ["type:loop", f"role:{_loop_role(rel, library)}"] + [f"folder:{q.lower().replace(' ', '-')}" for q in rel.parts[:-1]]
        out.append(Item(f"{src.name}:{rel}", name, [p], category="Loop", tags=tags, library=library))
    return out


# ---------------- generic libraries ----------------

def items_for(src: SourceConfig) -> list[Item]:
    """Every catalogue row the source should produce."""
    if src.type == "library":
        return _library_items(src)
    if src.type == "machine":
        return _machine_items(src)
    if src.type == "loops":
        return _loop_items(src)
    root, wavs = src.path, _wavs(src)
    rel = lambda p: p.relative_to(root)  # noqa: E731

    cat_of = lambda p: (src.category if src.category != "auto" else _guess_category(rel(p).parent.parts, "Synth")) if src.type == "oneshots" else None  # noqa: E731
    if src.type == "oneshots":
        # Field recordings, foley, FX. One row per file. The same file name often repeats in treatment folders (Clean, Tube, Half Speed...),
        # so a repeated name gets its folder appended, and the folders become tags (folder:outdoors, folder:tube) to search by.
        stems = {}
        for p in wavs:
            stems[p.stem] = stems.get(p.stem, 0) + 1
        return [Item(f"{src.name}:{rel(p)}", f"{p.stem} ({p.parent.name})" if stems[p.stem] > 1 else p.stem, [p], category=cat_of(p),
                     tags=[f"folder:{q.lower().replace(' ', '-')}" for q in rel(p).parts[:-1]], library=src.library or src.path.name)
                for p in wavs]
    if src.type == "wavetable":
        return [Item(f"{src.name}:{rel(p)}", p.stem, [p], category=cat_of(p)) for p in wavs]

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
            parsed = _split_note(p.stem)  # '<patch>-048_c2', '<patch> C3', '<patch>_C2_0002', ...
            groups.setdefault((p.parent, parsed[0] if parsed else p.stem), []).append(p)
        cut = _common_prefix_len([b for _, b in groups])
        out = []
        for (folder, base), files in sorted(groups.items()):
            picks, mode = _pick_notes(files, src.ref_midi)
            out.append(Item(f"{src.name}:{rel(folder) / base}", base[cut:] or base, picks, mode, len(files),
                            category=src.category if src.category != "auto" else _guess_category(rel(folder).parts, "Synth")))
        return out

    # multisample: one folder (that directly holds WAVs) = one preset
    folders: dict[Path, list[Path]] = {}
    for p in wavs:
        folders.setdefault(p.parent, []).append(p)
    out = []
    for f, files in sorted(folders.items()):
        picks, mode = _pick_notes(files, src.ref_midi)
        out.append(Item(f"{src.name}:{rel(f)}", f.name if str(rel(f)) != "." else root.name, picks, mode, len(files),
                        category=src.category if src.category != "auto" else _guess_category(rel(f).parts, "Synth")))
    return out


def plan_source(src: SourceConfig) -> tuple[list[Item], list[str]]:
    """(items, notes) for a source; notes are things worth telling the user (e.g. selected presets with no config)."""
    if src.type == "press":
        from . import press

        return press.plan(src)
    return items_for(src), []


def skip_reason(src: SourceConfig) -> str | None:
    if not src.stype.analyzable:
        return f"type '{src.type}' cannot be analysed yet"
    if src.type == "press":
        return None if src.path.is_dir() else f"path not found: {src.path}"
    return origin.source_missing(src)


def measure(files: list[Path], max_seconds: float) -> dict[str, float]:
    """Features of one origin file, or the mean over several (kits), each read through the cache (origin.local).
    Files that fail to load are skipped if others succeed."""
    results, last_err = [], None
    for f in files:
        try:
            chans, sr, dur = load(origin.local(f), max_seconds)
            results.append(extract(chans, sr, dur))
        except Exception as e:
            last_err = e
    if not results:
        raise last_err or ValueError("no audio")
    return {k: float(np.mean([r[k] for r in results])) for k in FEATURES + EXTRA}


def _signature(item: Item) -> tuple[float, int, str]:
    """Change detection from metadata only (item.files must already be origins, see _with_origins)."""
    stats = [origin.stat_sig(f) for f in (item.files or item.sig_files)]
    return (max(m for m, _ in stats), sum(size for _, size in stats), item.name)


def _with_origins(src: SourceConfig, item: Item) -> Item:
    """The item with its files mapped from src.path back to their origins (only differs for mounted sources)."""
    if src.mounts and item.files:
        item.files = [origin.resolve(src, f) for f in item.files]
    return item


def _preview(cfg: Config, item: Item, m: Measured) -> Path:
    """Audition file for a preset that has no single file to point at (a plugin render, a mapped multisample)."""
    d = cfg.catalogue.parent / "previews"
    d.mkdir(parents=True, exist_ok=True)
    path = d / (hashlib.sha1(item.id.encode()).hexdigest()[:16] + ".wav")
    sf.write(str(path), m.chans[:, : int(8 * m.sr)].T, m.sr, subtype="PCM_16")
    return path


def _kit_preview(cfg: Config, item: Item, max_seconds: float) -> Path:
    """Audition file for a kit: the hits that were measured, in order, ~1 s each, so the ear hears what the numbers describe."""
    parts, sr0 = [], 0
    for f in item.files:
        try:
            chans, sr, _ = load(origin.local(f), min(max_seconds, 1.0))
        except Exception:
            continue
        sr0 = sr0 or sr
        if sr != sr0:  # no resampler here; a rare odd file is left out of the preview only
            continue
        chans = np.atleast_2d(chans)
        chans = np.repeat(chans, 2, axis=0) if chans.shape[0] == 1 else chans[:2]
        n = int(sr * 1.0)
        parts.append(np.pad(chans[:, :n], ((0, 0), (0, int(sr * 0.15)))))
    if not parts:
        return item.files[0]
    d = cfg.catalogue.parent / "previews"
    d.mkdir(parents=True, exist_ok=True)
    path = d / (hashlib.sha1(item.id.encode()).hexdigest()[:16] + ".wav")
    sf.write(str(path), np.concatenate(parts, axis=1).T, sr0, subtype="PCM_16")
    return path


def analyze(cfg: Config, only: str | None = None, force: bool = False, on_event: Event | None = None,
            cancelled: Callable[[], bool] = lambda: False, limit: int | None = None) -> None:
    emit = on_event or (lambda *a, **k: None)
    conn = catalogue.connect(cfg.catalogue)
    have = {} if force else catalogue.known(conn)

    plans: list[tuple[SourceConfig, list[Item], set[str]]] = []
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
        plans.append((src, items[:limit] if limit else items, {i.id for i in items}))  # all_ids: prune against the full list, not the --limit slice
    emit("plan", total=sum(len(i) for _, i, _ in plans), sources={s.name: len(i) for s, i, _ in plans})

    for src, items, all_ids in plans:
        emit("source_start", source=src.name, total=len(items))
        counts = {"analysed": 0, "unchanged": 0, "failed": 0}
        for item in items:
            item = _with_origins(src, item)
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
                    feats, path, mode, n_samples = measure(item.files, cfg.max_seconds), (
                        _kit_preview(cfg, item, cfg.max_seconds) if item.mode == AVERAGE and len(item.files) > 1 else item.files[0]
                    ), item.mode, item.n_samples
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
        removed = catalogue.prune(conn, src.name, all_ids)
        conn.commit()
        emit("source_end", source=src.name, removed=removed, **counts)
    conn.close()
    _trim_cache(emit)
    emit("finished")


def _trim_cache(emit: Event) -> None:
    """Apply the shared cache's size cap once the run is over (fetches made here count as uses)."""
    try:
        from patch_press.cache import evict
    except ImportError:
        return
    try:
        for line in evict.trim().lines():
            emit("source_note", source="cache", note=line.strip())
    except Exception as e:  # never fail an analysis over housekeeping
        emit("source_note", source="cache", note=f"trim skipped: {type(e).__name__}: {e}")
