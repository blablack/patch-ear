"""Where a sound's audio comes from, and how to get a local copy of it.

A catalogue sound points at its ORIGIN, never at a local copy: a file on the NAS, or a member of a zip
on the NAS written as a path *through* the zip (`.../Pack.zip/Pack/Loops/a.wav`), so the walkers in
analyze.py keep using plain Path operations (relative_to, parent, stem) on it. A source can also be a
union of origins (`[source.mounts]`: subfolder name -> origin); its files are then listed under the
source's own path and `resolve` maps them back.

Listing and change detection read metadata only (a directory walk, a zip's central directory). The
audio itself comes through the shared patch-press cache (`patch_press.cache.sources`) and only when
it is read: measuring a new or changed sound, embedding, descriptor scoring, playback. Every such
fetch counts as a use of that cache entry, so the cache's least-recently-used eviction keeps what
patch-ear works with and drops what nobody touched; an evicted file is fetched again on next use.
Local copies live under <cache>/library/origins/<origin path>.
"""

from __future__ import annotations

import os
import shutil
import zipfile
from functools import lru_cache
from pathlib import Path

ZIP_SUFFIX = ".zip"


@lru_cache(maxsize=4096)
def _is_zip_file(p: str) -> bool:
    return p.lower().endswith(ZIP_SUFFIX) and os.path.isfile(p)


def split_zip(p: Path) -> tuple[Path, str] | None:
    """(zip file, member name inside it) when `p` is a path through a zip, else None. `member` is ""
    for the zip itself and a folder prefix for a path to a folder inside it."""
    parts = p.parts
    for i in range(1, len(parts) + 1):
        if parts[i - 1].lower().endswith(ZIP_SUFFIX) and _is_zip_file(str(Path(*parts[:i]))):
            return Path(*parts[:i]), "/".join(parts[i:])
    return None


@lru_cache(maxsize=64)
def _members(zip_path: Path) -> dict[str, zipfile.ZipInfo]:
    with zipfile.ZipFile(zip_path) as zf:
        return {i.filename: i for i in zf.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")}


def _list(root: Path) -> list[Path]:
    """Every file under `root` (a folder, a zip, or a folder inside a zip; a single file is itself), as origin paths."""
    if root.is_dir():
        return [p for p in root.rglob("*") if p.is_file()]
    if root.is_file() and not root.name.lower().endswith(ZIP_SUFFIX):
        return [root]
    z = split_zip(root)
    if not z:
        return []
    zip_path, prefix = z
    head = f"{prefix.rstrip('/')}/" if prefix else ""
    return [zip_path / name for name in _members(zip_path) if name.startswith(head)]


def exists(root: Path) -> bool:
    if root.is_dir() or (root.is_file() and not root.name.lower().endswith(ZIP_SUFFIX)):
        return True
    z = split_zip(root)
    if not z:
        return False
    zip_path, prefix = z
    head = f"{prefix.rstrip('/')}/" if prefix else ""
    return any(name.startswith(head) for name in _members(zip_path))


def list_files(src) -> list[Path]:
    """Every file of a source, under `src.path` (mounted sources: `src.path / <mount> / ...`). A mount name may
    have several parts (`Atlas/Electric Guitars`) and a mount may be a single file (named by its mount name)."""
    if not src.mounts:
        return _list(src.path)
    # A file under two mounts (a folder, and a file entry inside it) is listed once, by the deeper one.
    out: dict[Path, None] = {}
    for name in sorted(src.mounts, key=lambda n: len(Path(n).parts), reverse=True):
        origin = src.mounts[name]
        out.update(dict.fromkeys(src.path / name / f.relative_to(origin) for f in _list(origin)))
    return list(out)


def source_missing(src) -> str | None:
    """Why a source can't be listed (a missing folder/zip/mount), or None."""
    roots = list(src.mounts.values()) if src.mounts else [src.path]
    missing = [str(r) for r in roots if not exists(r)]
    return f"path not found: {', '.join(missing)}" if missing else None


def resolve(src, p: Path) -> Path:
    """The origin of a file listed by list_files (identity unless the source has mounts)."""
    if not src.mounts:
        return p
    rel = p.relative_to(src.path)
    for name in sorted(src.mounts, key=lambda n: len(Path(n).parts), reverse=True):  # the deepest mount that holds it
        if rel == Path(name) or Path(name) in rel.parents:
            return src.mounts[name] / rel.relative_to(name)
    raise KeyError(f"{p} is under no mount of source {src.name}")


def stat_sig(p: Path) -> tuple[float, int]:
    """(mtime, size) of an origin, from metadata only: a zip member takes its zip's mtime."""
    z = split_zip(p)
    if z:
        zip_path, member = z
        return zip_path.stat().st_mtime, _members(zip_path)[member].file_size
    st = p.stat()
    return st.st_mtime, st.st_size


def _local_roots() -> list[Path]:
    from .config import home

    roots = [home()]
    try:
        from patch_press.cache.renders import cache_root

        roots.append(cache_root())
    except ImportError:
        pass
    return roots


def is_local(p: Path) -> bool:
    """Files patch-ear made itself (previews) or that already live in the cache are read in place."""
    return any(p.is_relative_to(r) for r in _local_roots())


def local(p: str | Path) -> Path:
    """A readable local file with the bytes of origin `p`, fetched through the shared cache (a use)."""
    p = Path(p)
    if is_local(p):
        return p
    z = split_zip(p)
    try:
        from patch_press.cache import sources
    except ImportError:
        if z:
            raise RuntimeError(f"{p} is inside a zip: reading it needs patch-press (uv run --extra press)") from None
        return p
    if not sources.enabled():
        if z:
            raise RuntimeError(f"{p} is inside a zip and the patch-press cache is disabled (PATCH_PRESS_CACHE=0)")
        return p
    dest = sources.library_root() / "origins" / str(p).lstrip("/")
    if z:
        zip_path, member = z
        info = _members(zip_path)[member]

        def write(out: Path) -> None:
            with zipfile.ZipFile(zip_path) as zf, zf.open(info) as f, out.open("wb") as o:
                shutil.copyfileobj(f, o)

        sources.place_stream(sources.zip_member_ident(zip_path, info), info.file_size, dest, write)
    else:
        sources.place_file(p, dest)
    return dest
