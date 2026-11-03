"""TOML config shared by the analyzer CLI, the web UI and the MCP server.

The raw dict (as in the TOML file) is the source of truth; `parse` validates it into dataclasses.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

DEFAULT_CONFIG = "patch-ear.toml"


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
    SourceType("multisample", "Multisample library",
               "One subfolder = one preset (the notes of one instrument). Point at the folder whose subfolders are the presets. "
               "Like patch-press scan-library --type multisample.", ref_midi=True),
    SourceType("filename", "Multisample, flat folder",
               "One folder holds many presets, told apart by file name: <patch>-<midi>_<note>.wav "
               "(e.g. Wave Alchemy Essence bass oneshots).", ref_midi=True),
    SourceType("oneshots", "One-shots",
               "Every loose file is its own preset (folders of single-note samples). Like scan-oneshots."),
    SourceType("drumkit", "Drumkit (flat)",
               "Each folder holding loose hits is one kit (808 From Mars style). A kit's row is the average over its hits. "
               "Like scan-library --type drumkit."),
    SourceType("kit", "Kit library",
               "A folder per kit with instrument subfolders inside (Just Add Drums style). A kit's row is the average over its hits. "
               "Like scan-library --type kit."),
    SourceType("wavetable", "Wavetables",
               "Serum-format wavetables (2048-sample frames). Like scan-wavetables."),
    SourceType("press", "patch-press configs",
               "A patch-press Configs folder (<library>/<subfolders>/<preset>.yaml): the whole library, whatever the preset is made of. "
               "Audio comes from patch-press's own adapters: library, Kontakt/SFZ/Bitwig maps, wavetables, and CLAP/VST3 presets "
               "rendered one note each. The first folder is the library. Optionally restrict it to a subset file (config paths "
               "without .yaml, `*` allowed). Needs `uv sync --extra press`. Categories come from patch-press's tag rules.",
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

    @property
    def stype(self) -> SourceType:
        return TYPES_BY_ID[self.type]


@dataclass
class Config:
    catalogue: Path
    max_seconds: float = 8.0
    sources: list[SourceConfig] = field(default_factory=list)


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
        if not str(s.get("path", "")).strip():
            raise ValueError(f"source '{name}': path is required")
        if not str(s.get("category", "")).strip() and not TYPES_BY_ID[stype].auto_category:
            raise ValueError(f"source '{name}': category is required")
        plugin = str(s.get("plugin", "")).strip()
        if TYPES_BY_ID[stype].plugin and not plugin:
            raise ValueError(f"source '{name}': a plugin path is required for type {stype}")
        sources.append(SourceConfig(
            name=name, path=Path(s["path"]).expanduser(), category=str(s.get("category", "")).strip() or "auto", type=stype,
            ref_midi=int(s.get("ref_midi", 36)), plugin=Path(plugin).expanduser() if plugin else None,
            only_list=_rel(str(s.get("only_list", "")), base),
        ))
    return Config(catalogue=cat, max_seconds=float(raw.get("analysis", {}).get("max_seconds", 8.0)), sources=sources)


def load_config(path: str | Path = DEFAULT_CONFIG) -> Config:
    path = Path(path).expanduser()
    return parse(load_raw(path), path.resolve().parent)


def normalize_raw(raw: dict) -> dict:
    """Canonical TOML-able dict (drops empty optional fields, writes `type` not legacy keys)."""
    out = {
        "catalogue": {"path": str(raw.get("catalogue", {}).get("path", "catalogue.db"))},
        "analysis": {"max_seconds": float(raw.get("analysis", {}).get("max_seconds", 8.0))},
        "source": [],
    }
    for s in raw.get("source", []):
        stype = s.get("type") or _LEGACY_GROUP.get(s.get("group", ""), None) or "oneshots"
        item = {"name": str(s["name"]).strip(), "type": stype, "path": str(s["path"]).strip(), "category": str(s.get("category", "")).strip()}
        if not item["category"]:
            del item["category"]
        if TYPES_BY_ID.get(stype, SourceType("", "", "")).ref_midi:
            item["ref_midi"] = int(s.get("ref_midi", 36))
        if str(s.get("plugin", "")).strip():
            item["plugin"] = str(s["plugin"]).strip()
        if str(s.get("only_list", "")).strip():
            item["only_list"] = str(s["only_list"]).strip()
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
