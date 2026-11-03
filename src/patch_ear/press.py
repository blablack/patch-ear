"""patch-press scan configs as a patch-ear source (type "press").

patch-press already knows how to get audio out of every kind of source (library folders, Kontakt/SFZ maps, Bitwig
multisamples, wavetables, CLAP/VST3 presets). Re-using its adapters means patch-ear measures the audio patch-press
would export, and can render plugin presets (one note each) without building anything for a device.

A source points at a patch-press Configs tree: `<root>/<library>/<subfolders>/<preset>.yaml`. The first folder is the
library; every config under it is a preset. Nothing here knows about any device.

Requires the `press` extra:  uv sync --extra press
"""

from __future__ import annotations

from dataclasses import replace
from fnmatch import fnmatchcase
from pathlib import Path

import numpy as np
import yaml

from .config import SourceConfig
from .features import extract, measured_midi
from .items import AVERAGE, REPRESENTATIVE, SINGLE, Item, Measured, pick_category, pick_at_pitch, spread

HOLD_S, TOTAL_S = 4.0, 5.5  # a plugin note: held, then its tail; enough to measure decay and movement
PROBE_S = 3.0
UNSUPPORTED = {"granular"}  # source types the adapters here cannot turn into audio

_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def available() -> str | None:
    """None when patch-press is importable, else why not."""
    try:
        import patch_press  # noqa: F401
    except ImportError:
        return "type 'press' needs patch-press: run `uv sync --extra press`"
    return None


def read_patterns(path: Path) -> list[str]:
    """Lines of a subset file: config paths relative to the Configs root, without `.yaml`; `*` and `?` match."""
    return [ln.strip().removesuffix(".yaml") for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")]


def _kind(cfg: dict) -> str:
    """kit | wavetable | instrument, from the config itself (no device involved)."""
    source = cfg.get("source") or {}
    if source.get("type") == "wavetable":
        return "wavetable"
    if cfg.get("profile") == "drums" or source.get("drumkit") or source.get("files"):
        return "kit"
    return "instrument"


def plan(src: SourceConfig) -> tuple[list[Item], list[str]]:
    """Items for every config under the root (or the subset in `only_list`), plus notes about what was left out."""
    why = available()
    if why:
        raise RuntimeError(why)
    from patch_press.io.exporters.bento_index import derive_tags
    from patch_press.model.sample import Category

    patterns = read_patterns(src.only_list) if src.only_list else None
    if src.only_list and not src.only_list.is_file():
        raise FileNotFoundError(f"only_list not found: {src.only_list}")
    items: list[Item] = []
    unsupported: dict[str, int] = {}
    matched = set()
    for yml in sorted(src.path.rglob("*.yaml")):
        parts = yml.relative_to(src.path).with_suffix("").parts
        if len(parts) < 2:  # a config directly in the root has no library
            continue
        rel = "/".join(parts)
        if patterns is not None:
            hits = [pt for pt in patterns if fnmatchcase(rel, pt)]
            if not hits:
                continue
            matched.update(hits)
        cfg = yaml.load(yml.read_text(), Loader=_Loader) or {}
        stype = (cfg.get("source") or {}).get("type", "")
        if stype in UNSUPPORTED:
            unsupported[stype] = unsupported.get(stype, 0) + 1
            continue
        kind = _kind(cfg)
        coll, sub = parts[0], list(parts[1:-1])
        name = (cfg.get("output") or {}).get("name") or parts[-1]
        cat = {"kit": Category.DRUM, "wavetable": Category.WAVETABLE}.get(kind, Category.SYNTH)
        tags = derive_tags(cat, coll, sub, name)
        items.append(Item(
            id=f"{src.name}:{rel}", name=" / ".join([coll, *sub[-1:], name]), library=coll,
            tags=[*tags, f"type:{kind}"], category=pick_category(tags, kind),
            sig_files=[yml], capture=lambda max_seconds, yml=yml, ref=src.ref_midi: capture(yml, ref, max_seconds),
        ))
    notes = [f"{n} {t} configs skipped (cannot be measured yet)" for t, n in unsupported.items()]
    if patterns is not None:
        notes += [f"subset pattern matches nothing: {pt}" for pt in patterns if pt not in matched]
    return items, notes


def _trim_lead(chans: np.ndarray) -> np.ndarray:
    """Drop leading silence (plugin renders start with a lead-in)."""
    peak = float(np.abs(chans).max())
    if peak < 1e-6:
        return chans
    idx = np.nonzero(np.abs(chans).max(axis=0) > 0.01 * peak)[0]
    return chans[:, max(0, int(idx[0]) - 256):] if len(idx) else chans


def _sample_set(rc, ref_midi: int):
    from patch_press.io.adapters.bitwig import BitwigAdapter
    from patch_press.io.adapters.clap import CLAPAdapter
    from patch_press.io.adapters.library import LibraryAdapter
    from patch_press.io.adapters.mapped import MappedAdapter
    from patch_press.io.adapters.vst import VSTAdapter
    from patch_press.io.adapters.wavetable import WavetableAdapter
    from patch_press.config.schema import (
        BitwigSourceConfig, CLAPSourceConfig, LibrarySourceConfig, MappedSourceConfig, VSTSourceConfig, WavetableSourceConfig,
    )
    from patch_press.model.audio import AudioBuffer
    from patch_press.model.sample import Category, Sample, SampleSet

    src, name = rc.source, rc.name or None
    if isinstance(src, WavetableSourceConfig):
        return WavetableAdapter(src).capture(name=name)
    if isinstance(src, (CLAPSourceConfig, VSTSourceConfig)):
        adapter = (CLAPAdapter if isinstance(src, CLAPSourceConfig) else VSTAdapter)(src)
        adapter._apply_preset(src.preset or Path(str(src.plugin)).stem)  # what capture() does first; capture() itself keeps a 5 s lead-in
        cap = replace(rc.capture, velocities=[100], round_robins=1)

        def render(note: int) -> AudioBuffer:
            return adapter.render_note(note, 100, HOLD_S, TOTAL_S, cap.tempo_bpm, cap.sample_rate)

        # A preset can sound octaves away from the key pressed (a bass patch two octaves down): the key is a label too.
        # Render the reference key, pitch-track it, and re-render at the corrected key when it is a whole-octave off.
        note, audio = ref_midi, render(ref_midi)
        m = measured_midi(_trim_lead(audio.data)[:, : int(PROBE_S * audio.sample_rate)], audio.sample_rate)
        if m is not None:
            diff = m - ref_midi
            octaves = round(diff / 12) * 12
            if octaves and abs(diff - octaves) < 3 and 24 <= ref_midi - octaves <= 108:
                note = ref_midi - octaves
                audio = render(note)
        return SampleSet(name=name or "", category=Category.SYNTH, samples=[Sample(note=note, velocity=100, round_robin=1, audio=audio)])
    if isinstance(src, (BitwigSourceConfig, MappedSourceConfig)):
        adapter = BitwigAdapter(src) if isinstance(src, BitwigSourceConfig) else MappedAdapter(src)
        return adapter.capture(name=name, max_round_robins=1, note_step=1, keep_velocity_layers=False)
    if isinstance(src, LibrarySourceConfig):
        return LibraryAdapter(src).capture(name=name, max_round_robins=1, note_step=1)
    raise TypeError(f"unsupported source {type(src).__name__}")


def _features(chans: np.ndarray, sr: int, max_seconds: float) -> dict[str, float]:
    chans = _trim_lead(chans)
    return extract(chans[:, : int(max_seconds * sr)], sr, chans.shape[1] / sr)


def capture(yml: Path, ref_midi: int, max_seconds: float) -> Measured:
    """Audio for one preset via patch-press's own adapter, then measured at the reference pitch (a kit: averaged over its hits)."""
    from patch_press.config.loader import load_config
    from patch_press.model.sample import Category

    sset = _sample_set(load_config(yml), ref_midi)
    samples = sset.samples
    if not samples:
        raise ValueError("source produced no samples")
    from .features import FEATURES, EXTRA

    if sset.category == Category.DRUM:  # a kit: its character is the whole kit
        picked = spread(samples)
        feats = [_features(s.audio.data, s.audio.sample_rate, max_seconds) for s in picked]
        mean = {k: float(np.mean([f[k] for f in feats])) for k in FEATURES + EXTRA}
        first = picked[0]
        return Measured(mean, _trim_lead(first.audio.data), first.audio.sample_rate, len(samples), AVERAGE)

    by_note: dict[int, object] = {}  # one candidate per note: the loudest velocity layer
    for s in samples:
        if s.note not in by_note or s.velocity > by_note[s.note].velocity:
            by_note[s.note] = s
    cands = sorted(by_note.items())

    def probe(s) -> float | None:
        return measured_midi(_trim_lead(s.audio.data)[:, : int(PROBE_S * s.audio.sample_rate)], s.audio.sample_rate)

    chosen = pick_at_pitch(cands, ref_midi, probe)
    chans, sr = _trim_lead(chosen.audio.data), chosen.audio.sample_rate
    return Measured(_features(chosen.audio.data, sr, max_seconds), chans, sr, len(samples), REPRESENTATIVE if len(samples) > 1 else SINGLE)
