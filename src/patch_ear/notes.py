"""The user's own notes, picks and ear verdicts about sounds. Lives in its OWN file (notes.db next to the catalogue) so that re-analysing or
rebuilding the catalogue can never wipe it. This is the only thing the MCP server writes.

kinds: note (free text), like, reject, pick (a choice for an album slot, e.g. album "city", slot "hard bass"; the newest pick per
(album, slot) is the current one), tag_ok / tag_wrong (the user's ear says a mood/use tag on that sound is right / wrong; text = the tag).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from . import config

KINDS = ("note", "like", "reject", "pick", "tag_ok", "tag_wrong")
SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    n INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    patch_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    album TEXT NOT NULL DEFAULT '',
    slot TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_notes_patch ON notes(patch_id);
"""


def path() -> Path:
    return config.home() / "notes.db"


def connect() -> sqlite3.Connection:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def add(conn: sqlite3.Connection, patch_id: str, kind: str, text: str = "", album: str = "", slot: str = "") -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if kind == "pick" and not (album and slot):
        raise ValueError("a pick needs an album and a slot (e.g. album 'city', slot 'hard bass')")
    if kind in ("tag_ok", "tag_wrong") and not text:
        raise ValueError("tag_ok / tag_wrong need the tag in `text`")
    conn.execute("INSERT INTO notes (ts, patch_id, kind, album, slot, text) VALUES (?,?,?,?,?,?)",
                 (int(time.time()), patch_id, kind, album.strip(), slot.strip().lower(), text.strip()))
    conn.commit()
    return {"saved": kind, "patch_id": patch_id, "album": album, "slot": slot, "text": text}


def listing(conn: sqlite3.Connection, patch_id: str = "", album: str = "", kind: str = "", limit: int = 50) -> list[dict]:
    q, args, where = "SELECT * FROM notes", [], []
    for col, v in (("patch_id", patch_id), ("album", album), ("kind", kind)):
        if v:
            where.append(f"{col}=?")
            args.append(v)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY n DESC LIMIT ?"
    return [dict(r) for r in conn.execute(q, [*args, limit])]


def palette(conn: sqlite3.Connection, album: str) -> list[dict]:
    """The current pick per slot of an album (the newest pick wins), oldest slot first."""
    rows = conn.execute("SELECT * FROM notes WHERE kind='pick' AND album=? ORDER BY n", (album,)).fetchall()
    latest: dict[str, dict] = {}
    for r in rows:
        latest[r["slot"]] = dict(r)
    return list(latest.values())


def tag_feedback(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """{tag: {"ok": n, "wrong": n}} from the user's ear verdicts: how far the zero-shot mood/use tags can be trusted."""
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT text, kind, COUNT(*) c FROM notes WHERE kind IN ('tag_ok','tag_wrong') GROUP BY text, kind"):
        out.setdefault(r["text"], {"ok": 0, "wrong": 0})["ok" if r["kind"] == "tag_ok" else "wrong"] = r["c"]
    return out
