"""patch-press scan configs as a patch-ear source (type "deluge").

patch-press already knows how to get audio out of every kind of source (library folders, Kontakt/SFZ maps, Bitwig
multisamples, wavetables, CLAP/VST3 presets). Re-using its adapters means patch-ear measures exactly the audio the
device build would use, and can render plugin presets (one note each) without a card or a build.

Requires the `press` extra:  uv sync --extra press
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import numpy as np
import yaml

from .config import SourceConfig
from .features import extract, measured_midi
from .items import AVERAGE, REPRESENTATIVE, SINGLE, Item, Measured, bento_category, pick_at_pitch, spread

HOLD_S, TOTAL_S = 4.0, 5.5  # a plugin note: held, then its tail; enough to measure decay and movement
PROBE_S = 3.0


def available() -> str | None:
    """None when patch-press is importable, else why not."""
    try:
        import patch_press  # noqa: F401
    except ImportError:
        return "type 'deluge' needs patch-press: run `uv sync --extra press`"
    return None


def read_lines(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")]


def _field(text: str, field: str) -> str:
    """One scalar from a config's `output:` block (same reading rule prepare-sd-cards uses)."""
    inside = False
    for line in text.splitlines():
        if line.rstrip() == "output:":
            inside = True
            continue
        if inside:
            if line and not line[0].isspace():
                break
            m = re.match(rf'^\s+{field}:\s*"?(?P<v>.*?)"?\s*$', line)
            if m:
                return m.group("v")
    return ""


def index_configs(root: Path) -> dict[str, Path]:
    """Deluge identity ('SYNTHS|KITS/<collection>/<subfolder>/<name>') -> the config that builds it.

    The identity is exactly what patch-press's DelugeExporter derives from the config's output name/subfolder, and the
    collection is the config's top folder under `root`. Whether it lands in SYNTHS or KITS is decided at export time, so
    both are indexed and the caller's selection list picks."""
    from patch_press.config.schema import OutputConfig
    from patch_press.io.exporters import get_exporter

    exporter, virtual = get_exporter("deluge"), Path("/_deluge")
    index: dict[str, Path] = {}
    for yml in sorted(root.rglob("*.yaml")):
        parts = yml.relative_to(root).parts
        if len(parts) < 2:
            continue
        text = yml.read_text()
        out = OutputConfig(name=_field(text, "name") or yml.stem, subfolder=_field(text, "subfolder"), folder=_field(text, "folder"))
        for p in exporter.expected_outputs(out, virtual / parts[0]):
            rel = str(p.relative_to(virtual))
            if rel.lower().endswith(".xml"):
                index.setdefault(rel[:-4], yml)
    return index


def plan(src: SourceConfig) -> tuple[list[Item], list[str]]:
    """Items for every selected preset that has a config, plus notes about the ones that don't."""
    why = available()
    if why:
        raise RuntimeError(why)
    if not src.only_list:
        raise RuntimeError("type 'deluge' needs only_list (the selection, e.g. DELUGE_PATCHES): the config alone does not say SYNTHS or KITS")
    from patch_press.io.exporters.bento_index import derive_tags
    from patch_press.model.sample import Category

    index = index_configs(src.path)
    items: list[Item] = []
    missing: list[str] = []
    for entry in read_lines(src.only_list):
        yml = index.get(entry)
        if yml is None:
            missing.append(entry)
            continue
        parts = entry.split("/")
        root, coll, sub, name = parts[0], parts[1], parts[2:-1], parts[-1]
        stype = ((yaml.safe_load(yml.read_text()) or {}).get("source") or {}).get("type", "")
        if root == "KITS":
            cat, kind = Category.DRUM, "kit"
        elif stype == "wavetable":
            cat, kind = Category.WAVETABLE, "wavetable"
        else:
            cat, kind = Category.SYNTH, "instrument"
        tags = derive_tags(cat, coll, sub, name)
        display = " / ".join([coll, *sub[-1:], name])
        items.append(Item(
            id=f"{src.name}:{entry}", name=display, tags=[*tags, f"type:{kind}"], category=bento_category(tags, kind),
            sig_files=[yml], capture=lambda max_seconds, yml=yml, ref=src.ref_midi: capture(yml, ref, max_seconds),
        ))
    notes = [f"{len(missing)} of the selected presets have no config yet (not scanned): e.g. {', '.join(missing[:3])}"] if missing else []
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
