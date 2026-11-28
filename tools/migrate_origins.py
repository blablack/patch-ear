"""One-off: point existing catalogue rows at their origins (NAS files, zip members) instead of local copies.

    uv run --extra clap --extra press python tools/migrate_origins.py            # report only
    uv run --extra clap --extra press python tools/migrate_origins.py --apply    # back up the catalogue, then write

After a source is repointed from a local copy (an unpacked folder, a device's staging tree) to its origin, its ids are
unchanged but every row's `path` and change signature (mtime) are not, so `analyze` would re-measure it and `embed` /
`descriptors` would redo it, all on identical audio. This plans every source from the current config (metadata only, no
audio read) and, for each existing row whose files match by size and file name, moves `path` and `mtime` to the origin
and rebases the embedding and descriptor rows that were computed from the old path. Rows that don't match are left
alone: the next `analyze` measures them as changed. Kit rows whose path is a generated preview keep it.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

from patch_ear.analyze import _signature, _with_origins, plan_source, skip_reason
from patch_ear.config import load_config


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default=None)
    ap.add_argument("--apply", action="store_true", help="write the changes (after backing up the catalogue)")
    a = ap.parse_args()
    cfg = load_config(a.config) if a.config else load_config()
    previews = cfg.catalogue.parent / "previews"
    conn = sqlite3.connect(cfg.catalogue)
    rows = {r[0]: r[1:] for r in conn.execute("SELECT id, path, mtime, size, name FROM patches")}

    updates: list[tuple[str, str, str, float]] = []  # (id, old path, new path, new mtime)
    mismatched, total = 0, 0
    for src in cfg.sources:
        if src.type == "press":
            continue
        # Only a source with rows outside its own origins needs planning (planning walks the source over the NAS).
        roots = [str(r) for r in (src.mounts.values() if src.mounts else [src.path])] + [str(previews)]
        paths = [r[0] for r in conn.execute("SELECT path FROM patches WHERE source=?", (src.name,))]
        if all(any(pth.startswith(r) for r in roots) for pth in paths) or skip_reason(src):
            continue
        items, _ = plan_source(src)
        n_src = 0
        for item in items:
            row = rows.get(item.id)
            if row is None:
                continue
            total += 1
            old_path, old_mtime, old_size, _ = row
            item = _with_origins(src, item)
            mtime, size, _ = _signature(item)
            new_path = old_path if Path(old_path).is_relative_to(previews) else str(item.files[0])
            if size != old_size or Path(new_path).name != Path(old_path).name:
                mismatched += 1
                continue
            if (new_path, mtime) != (old_path, old_mtime):
                updates.append((item.id, old_path, new_path, mtime))
                n_src += 1
        if n_src:
            print(f"{src.name:34} {n_src:5} row(s) to rebase", flush=True)

    print(f"\n{total} catalogue rows planned, {len(updates)} to rebase onto their origins, {mismatched} left for analyze "
          f"(different size or file: re-measured as changed)")
    if not a.apply or not updates:
        if updates:
            print("report only; rerun with --apply to write")
        return 0

    backup = cfg.catalogue.with_name(cfg.catalogue.name + ".bak-pre-origins")
    if backup.exists():
        sys.exit(f"{backup} already exists: move it away first (it is the backup of an earlier migration)")
    conn.close()
    shutil.copy2(cfg.catalogue, backup)
    print(f"backup: {backup}")
    conn = sqlite3.connect(cfg.catalogue)
    with conn:
        for pid, old, new, mtime in updates:
            conn.execute("UPDATE patches SET path=?, mtime=? WHERE id=?", (new, mtime, pid))
            for table in ("embeddings", "descriptors"):
                conn.execute(f"UPDATE {table} SET path=? WHERE id=? AND path=?", (new, pid, old))  # noqa: S608 -- fixed table names
    print(f"rebased {len(updates)} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
