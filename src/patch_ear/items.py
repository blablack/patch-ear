"""What a source produces (Item) and how one sample is chosen to represent a preset. No I/O of its own."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

import numpy as np

SINGLE, REPRESENTATIVE, AVERAGE = 0, 1, 2  # stored in catalogue `representative`
MAX_AVERAGED = 24  # a kit is measured over at most this many hits (evenly spaced)
T = TypeVar("T")


@dataclass
class Measured:
    """Result of a preset that is not just files on disk (e.g. a plugin render): features plus audio to audition."""

    features: dict[str, float]
    chans: np.ndarray | None = None  # saved as the audition preview, since there is no file to point at
    sr: int = 0
    n_samples: int = 1
    mode: int = SINGLE


@dataclass
class Item:
    id: str
    name: str
    files: list[Path] = field(default_factory=list)  # measured files: one, or several to average
    mode: int = SINGLE
    n_samples: int = 1  # samples in the preset overall
    category: str | None = None  # overrides the source's category (e.g. from the library's own tags)
    tags: list[str] = field(default_factory=list)
    capture: Callable[[float], Measured] | None = None  # for presets that need work to get audio (render, mapping)
    sig_files: list[Path] = field(default_factory=list)  # what to stat for change detection when there is no `files`
    library: str = ""  # the library the preset comes from (the folder under the Configs root); empty = the source's name


def spread(files: list, n: int = MAX_AVERAGED) -> list:
    if len(files) <= n:
        return files
    return [files[int(i)] for i in np.linspace(0, len(files) - 1, n)]


def pick_category(tags: list[str], fallback: str) -> str:
    """'Synth' is the generic tag; prefer a more specific one when the patch has several."""
    specific = [t for t in tags if t != "Synth"]
    return specific[0] if specific else (tags[0] if tags else fallback)


def pick_at_pitch(cands: list[tuple[int, T]], ref_midi: int, probe: Callable[[T], float | None]) -> T:
    """The candidate at the reference pitch. Note LABELS are not trusted (a "C1" is 33 Hz in one library and 65 Hz in
    another): the candidate nearest the label is pitch-tracked via `probe` and any whole-octave disagreement is applied
    to the label scale before choosing."""

    def nearest(target: float) -> tuple[int, T]:
        return min(cands, key=lambda c: abs(c[0] - target))

    first = nearest(ref_midi)
    try:
        m = probe(first[1])
    except Exception:
        m = None
    if m is not None:
        diff = m - first[0]
        octaves = round(diff / 12) * 12
        if abs(diff - octaves) < 3:  # a consistent whole-octave shift, not detector noise
            return nearest(ref_midi - octaves)[1]
    return first[1]
