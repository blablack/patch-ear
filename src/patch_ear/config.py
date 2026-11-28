"""TOML config shared by the analyzer CLI, the web UI and the MCP server.

The raw dict (as in the TOML file) is the source of truth; `parse` validates it into dataclasses.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

CONFIG_NAME = "patch-ear.toml"


def home() -> Path:
    """The one folder patch-ear keeps its state in: config, catalogue and audio previews.
    PATCH_EAR_HOME, else ./data. In the cloud this is the volume to mount; library folders stay absolute paths (mounts)."""
    return Path(os.environ.get("PATCH_EAR_HOME") or "data").expanduser().resolve()


def default_config() -> Path:
    """PATCH_EAR_CONFIG if set, else <home>/patch-ear.toml."""
    return Path(os.environ["PATCH_EAR_CONFIG"]).expanduser() if os.environ.get("PATCH_EAR_CONFIG") else home() / CONFIG_NAME


@dataclass(frozen=True)
class SourceType:
    id: str
    label: str
    help: str
    analyzable: bool = True
    plugin: bool = False  # needs a plugin binary + a preset folder
    ref_midi: bool = False  # picks one representative sample per preset, nearest this MIDI note
    has_list: bool = False  # supports `only_list` (a text file restricting which presets are analysed)
    auto_category: bool = False  # category comes from the data itself, so the field is optional


# Mirrors patch-press's source shapes (see ../patch-press/CLAUDE.md). "filename" is patch-ear specific.
SOURCE_TYPES: list[SourceType] = [
    SourceType("library", "Library folder (auto)",
               "Point at a folder you want covered; the shape is decided per folder. Anything under a folder called \"Kits\" "
               "(or \"... Kit\") is a kit (the folder below it = one kit, averaged over its hits); a folder whose file names carry "
               "notes is one multisample; every other file is a one-shot. Nothing is filtered out, so point it only at folders "
               "you want. The library name is the folder's name unless you set one.", ref_midi=True, auto_category=True),
    SourceType("multisample", "Multisample library",
               "One subfolder = one preset (the notes of one instrument). Point at the folder whose subfolders are the presets. "
               "Like patch-press scan-library --type multisample. Category is read from the path words (bass, pad...) unless you set one.",
               ref_midi=True, auto_category=True),
    SourceType("filename", "Multisample, flat folder",
               "One folder holds many presets, told apart by file name: the patch name followed by the note "
               "(<patch>-<midi>_<note>.wav, <patch> C3.wav, <patch>_C2_0002.wav...). Category is read from the path words unless you set one.",
               ref_midi=True, auto_category=True),
    SourceType("oneshots", "One-shots",
               "Every loose file is its own preset (folders of single-note samples). Like scan-oneshots. Category is read from the path "
               "words unless you set one.", auto_category=True),
    SourceType("drumkit", "Drumkit (flat)",
               "Each folder holding loose hits is one kit (808 From Mars style). A kit's row is the average over its hits. "
               "Like scan-library --type drumkit."),
    SourceType("kit", "Kit library",
               "A folder per kit with instrument subfolders inside (Just Add Drums style). A kit's row is the average over its hits. "
               "Like scan-library --type kit."),
    SourceType("machine", "Drum machine (per role)",
               "A drum-machine pack (point at the pack root, with loops/kits/grooves excluded). Every hit is sorted by role from its "
               "folder and file names (kick, snare, clap, hat, tom, cymbal) and each role is one row per machine (averaged over up to "
               "24 hits; clean and coloured versions are kept apart when both exist). The category is the role, so grit and weight are "
               "ranked among kicks, among snares and so on, which makes machines comparable.",
               auto_category=True),
    SourceType("loops", "Loops",
               "Every WAV is one loop row (category Loop). Tempo and key are NOT measured: these are professional loops that get synced "
               "to the project. What matters is character (grit, weight, movement, mood). A role tag (drums, bass, melodic, drone, texture, fx, "
               "synth) comes from the folder and file names.", auto_category=True),
    SourceType("wavetable", "Wavetables",
               "Serum-format wavetables (2048-sample frames). Like scan-wavetables."),
    SourceType("press", "patch-press configs",
               "A patch-press Configs folder (<library>/<subfolders>/<preset>.yaml): the whole library, whatever the preset is made of. "
               "Audio comes from patch-press's own adapters: library, Kontakt/SFZ/Bitwig maps, wavetables, and CLAP/VST3 presets "
               "rendered one note each. The first folder is the library. Optionally restrict it to a subset file (config paths "
               "without .yaml, `*` allowed). Needs `uv sync --extra press`. Categories come from patch-press's tag rules unless the source "
               "sets a category, which then applies to every preset it selects (use a subset file per category).",
               ref_midi=True, has_list=True, auto_category=True),
    SourceType("bitwig", "Bitwig .multisample",
               "Bitwig multisample libraries (ZIP + XML). Configuration is stored; the analyzer does not read them yet.",
               analyzable=False),
    SourceType("clap", "CLAP plugin presets",
               "A .clap plugin plus a folder of its presets (.clap-preset / .fxp / u-he .h2p). "
               "Configuration is stored; rendering presets for analysis is not built yet.", analyzable=False, plugin=True),
    SourceType("vst3", "VST3 plugin (patch-probe)",
               "A .vst3 plugin plus the folder of patch-probe YAML snapshots. "
               "Configuration is stored; rendering for analysis is not built yet.", analyzable=False, plugin=True),
]
TYPES_BY_ID = {t.id: t for t in SOURCE_TYPES}

# Pre-0.2 configs used `group`; map them to types.
_LEGACY_GROUP = {"file": "oneshots", "folder": "multisample", "filename": "filename"}


@dataclass
class SourceConfig:
    name: str
    path: Path  # library folder, or the preset folder for plugin types
    category: str
    type: str = "oneshots"
    ref_midi: int = 36
    plugin: Path | None = None  # plugin binary, plugin types only
    only_list: Path | None = None  # text file, one patch path per line; others are ignored
    exclude: list[str] = field(default_factory=list)  # folder names/paths (fnmatch, relative to `path`) to leave out: loops, FX, phrases...
    library: str = ""  # name shown as the preset's library; empty = the source folder's name
    # A source that is a union of origins: subfolder name -> origin (a NAS folder, a zip, or a folder inside a zip).
    # Its files are listed under `path` (then only a label: "/mounts/<name>" when the config gives none) as <path>/<name>/...,
    # so ids stay "<source>:<name>/<rest>". See origin.py.
    mounts: dict[str, Path] = field(default_factory=dict)

    @property
    def stype(self) -> SourceType:
        return TYPES_BY_ID[self.type]


@dataclass
class Config:
    catalogue: Path
    max_seconds: float = 8.0
    sources: list[SourceConfig] = field(default_factory=list)
    cache_dir: Path | None = None  # shared patch-press render/source cache; PATCH_PRESS_CACHE_DIR wins over this


def default_raw() -> dict:
    return {"catalogue": {"path": "catalogue.db"}, "analysis": {"max_seconds": 8.0}, "source": []}


def load_raw(path: str | Path) -> dict:
    path = Path(path).expanduser()
    if not path.exists():
        return default_raw()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return default_raw() | raw


def _rel(value: str, base: Path) -> Path | None:
    """Optional path; relative ones resolve from the config file's folder."""
    if not value.strip():
        return None
    p = Path(value.strip()).expanduser()
    return p if p.is_absolute() else base / p


def parse(raw: dict, base: Path) -> Config:
    cat = Path(raw.get("catalogue", {}).get("path", "catalogue.db")).expanduser()
    if not cat.is_absolute():
        cat = base / cat
    sources, names = [], set()
    for s in raw.get("source", []):
        name = str(s.get("name", "")).strip()
        if not name or ":" in name:
            raise ValueError("every source needs a name (without ':')")
        if name in names:
            raise ValueError(f"duplicate source name '{name}'")
        names.add(name)
        stype = s.get("type") or _LEGACY_GROUP.get(s.get("group", ""), None) or s.get("kind", "oneshots")
        if stype == "wav_library":
            stype = "oneshots"
        if stype not in TYPES_BY_ID:
            raise ValueError(f"source '{name}': unknown type '{stype}'")
        mounts = {str(k): Path(str(v)).expanduser() for k, v in (s.get("mounts") or {}).items()}
        if not str(s.get("path", "")).strip() and not mounts:
            raise ValueError(f"source '{name}': path (or mounts) is required")
        if not str(s.get("category", "")).strip() and not TYPES_BY_ID[stype].auto_category:
            raise ValueError(f"source '{name}': category is required")
        plugin = str(s.get("plugin", "")).strip()
        if TYPES_BY_ID[stype].plugin and not plugin:
            raise ValueError(f"source '{name}': a plugin path is required for type {stype}")
        sources.append(SourceConfig(
            name=name, path=Path(str(s.get("path") or f"/mounts/{name}")).expanduser(), mounts=mounts,
            category=str(s.get("category", "")).strip() or "auto", type=stype,
            ref_midi=int(s.get("ref_midi", 36)), plugin=Path(plugin).expanduser() if plugin else None,
            only_list=_rel(str(s.get("only_list", "")), base), library=str(s.get("library", "")).strip(),
            exclude=[str(x).strip() for x in s.get("exclude", []) if str(x).strip()],
        ))
    cache = str(raw.get("cache", {}).get("dir", "")).strip()
    cache_dir = _rel(cache, base) if cache else None
    return Config(catalogue=cat, max_seconds=float(raw.get("analysis", {}).get("max_seconds", 8.0)), sources=sources,
                  cache_dir=Path(cache_dir) if cache_dir else None)


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path).expanduser() if path else default_config()
    cfg = parse(load_raw(path), path.resolve().parent)
    if cfg.cache_dir:
        # Read by patch_press.cache when the press source renders; an explicit env var wins.
        os.environ.setdefault("PATCH_PRESS_CACHE_DIR", str(cfg.cache_dir))
    return cfg


def normalize_raw(raw: dict) -> dict:
    """Canonical TOML-able dict (drops empty optional fields, writes `type` not legacy keys)."""
    out = {
        "catalogue": {"path": str(raw.get("catalogue", {}).get("path", "catalogue.db"))},
        "analysis": {"max_seconds": float(raw.get("analysis", {}).get("max_seconds", 8.0))},
        "source": [],
    }
    if str(raw.get("cache", {}).get("dir", "")).strip():
        out["cache"] = {"dir": str(raw["cache"]["dir"]).strip()}
    for s in raw.get("source", []):
        stype = s.get("type") or _LEGACY_GROUP.get(s.get("group", ""), None) or "oneshots"
        item = {"name": str(s["name"]).strip(), "type": stype, "path": str(s.get("path", "")).strip(), "category": str(s.get("category", "")).strip()}
        if not item["category"]:
            del item["category"]
        if not item["path"]:
            del item["path"]
        if TYPES_BY_ID.get(stype, SourceType("", "", "")).ref_midi:
            item["ref_midi"] = int(s.get("ref_midi", 36))
        if str(s.get("plugin", "")).strip():
            item["plugin"] = str(s["plugin"]).strip()
        if str(s.get("library", "")).strip():
            item["library"] = str(s["library"]).strip()
        if s.get("exclude"):
            item["exclude"] = [str(x).strip() for x in s["exclude"] if str(x).strip()]
        if str(s.get("only_list", "")).strip():
            item["only_list"] = str(s["only_list"]).strip()
        if s.get("mounts"):
            item["mounts"] = {str(k): str(v).strip() for k, v in s["mounts"].items()}
        out["source"].append(item)
    return out


def save_raw(path: str | Path, raw: dict) -> dict:
    """Validate then write. Comments in a hand-edited file are not preserved."""
    path = Path(path).expanduser()
    norm = normalize_raw(raw)
    parse(norm, path.resolve().parent)  # raises ValueError on bad input
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_bytes(tomli_w.dumps(norm).encode())
    tmp.replace(path)
    return norm
