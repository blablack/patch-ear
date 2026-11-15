"""MCP server: read-only queries over the catalogue. Does NO audio analysis."""

from __future__ import annotations

import os
import socket
import sys
from urllib.parse import urlencode, urlparse

from mcp.server.mcpserver import MCPServer

from . import catalogue, notes, query
from .config import load_config

mcp = MCPServer("patch-ear")


def _conn():
    cfg = load_config()
    if not cfg.catalogue.exists():
        raise RuntimeError(f"catalogue not found at {cfg.catalogue}; run `patch-ear analyze` first")
    return catalogue.connect(cfg.catalogue, readonly=True)


@mcp.tool()
def list_categories() -> dict:
    """Categories in the catalogue with patch counts, plus the sortable/filterable measurement names."""
    conn = _conn()
    counts = {r["category"]: r["n"] for r in conn.execute("SELECT category, COUNT(*) n FROM patches GROUP BY category")}
    tags: dict[str, int] = {}
    for r in catalogue.load_ranked(conn):
        for t in r.get("tags", []):
            tags[t] = tags.get(t, 0) + 1
    libraries = {r["library"]: r["n"] for r in conn.execute("SELECT library, COUNT(*) n FROM patches GROUP BY library")}
    return {"categories": counts, "libraries": libraries, "tags": tags, "measurements": query.SORTABLE,
            "notes": "grit and weight are 0-100 scores (percentile within category). Other measurements are raw values."}


@mcp.tool()
def search_patches(category: str | None = None, tag: str | None = None, library: str | None = None, filters: dict[str, list[float | None]] | None = None,
                   mood: str | list[str] | None = None, use: str | list[str] | None = None,
                   sort_by: str = "grit", descending: bool = True, limit: int = 15) -> list[dict]:
    """Rank patches by measured audio character, within a category (percentiles are per category) and/or a tag.

    category: the primary category (the most specific tag, e.g. Bass, Pad, Drum, Atmosphere).
    tag: any of a patch's tags (a patch can have several, e.g. Synth + Lead).

    library: the library a preset comes from (case-insensitive substring, e.g. "drumtrax"); list_categories shows them.

    filters: {measurement: [min_or_null, max_or_null]}, e.g. {"centroid_hz": [null, 400], "side_mid_db": [null, -40]}.
    mood / use: keep only sounds tagged with these (all of them if a list). Mood tags: aggressive, calm, dark, bright, warm, cold,
    tense, dreamy, melancholic, epic, minimal, hypnotic, industrial, organic, eerie, gritty, clean, heavy, delicate, driving,
    glacial, vintage, futuristic, chaotic. Use tags: quiet-intro, tension-build, heavy-climax, outro-fade, ambient-bed, main-pulse,
    city-night, industrial-machinery, cinematic-score, transition. They come from a zero-shot audio-text model and are relative to
    the sound's own category ("aggressive" for a kick = among kicks); a proposal, not a verdict.
    sort_by: grit, weight, intensity, or any raw measurement (centroid_hz, low_ratio, flatness, lfo_rate_hz, ...).
    CALM vs AGGRESSIVE: sort_by "intensity" (0-100 within the category; descending = most aggressive/energetic, ascending = calmest). It
    averages the arousal and mood_aggressive ranks, which agreed with the audio-text tags on loops and basses and with what the loop
    names suggest (drones/ambient at the bottom, noise beats at the top). Ranks are within a category, so "calm kick" means calm among kicks.
    By ear it held at the extremes and for one mid-scale set of drum loops (29/55/67), but another set (36/57/69) sounded the same level,
    so read it as coarse bands (calm, middle, aggressive), not a fine ranking in the middle.
    TRAINED SCORES (usable in sort_by and filters; absent for sounds not yet scored, which then sort last / never match a filter).
    Models trained on listener-labelled music: best on loops and musical sounds; on single hits rank within the category, do not read
    absolute values.
      Trustworthy: arousal (1-9, calm to energetic), valence (1-9, dark/sad to bright/happy), mood_aggressive, mood_party, mood_happy,
      and the timbre scores (about 0-100, trained on sound effects): timbre_hardness (attack hardness, NOT distortion: use grit for that),
      timbre_depth, timbre_brightness, timbre_roughness, timbre_warmth, timbre_sharpness, timbre_boominess.
      Weak: mood_relaxed (it rises with aggressive loops too; use low intensity for calm instead).
      Not informative here (near 0 or near 1 for almost everything): mood_sad, mood_danceable, mood_electronic, mood_instrumental,
      mood_acoustic, mood_tonal, and timbre_reverb (only 0 or 1). Whole-kit rows (category Drum) have no timbre scores: use the role rows.
    Results show intensity_pct, arousal, valence and timbre.
    Results come from measurements, not preset names. The user's ears make the final call."""
    rows = catalogue.load_ranked(_conn(), category)
    return [query.summarize(r) for r in query.search(rows, filters, sort_by, descending, limit, tag, library, mood, use)]


@mcp.tool()
def describe_patch(patch_id: str) -> dict:
    """Full measurement card for one patch: raw values, percentiles within its category, labels, file path."""
    rows = [r for r in catalogue.load_ranked(_conn()) if r["id"] == patch_id]
    if not rows:
        raise ValueError(f"unknown id '{patch_id}'")
    return query.summarize(rows[0], full=True)


@mcp.tool()
def similar_to(patch_id: str, n: int = 10) -> list[dict]:
    """Patches whose measured character is closest to the given one (across all categories)."""
    rows = catalogue.load_ranked(_conn())
    return [query.summarize(r) | {"distance": round(d, 2)} for r, d in query.similar(rows, patch_id, n)]


@mcp.tool()
def compare_patches(patch_ids: list[str]) -> dict:
    """Side-by-side measurements for 2-8 patches (e.g. two candidate basses, or the same role on several drum machines).
    Returns, per measurement, one value per patch, plus labels and the category each is ranked in. Percentiles are within
    each patch's OWN category, so only compare scores directly between patches of the same category (kick vs kick)."""
    if not 2 <= len(patch_ids) <= 8:
        raise ValueError("give between 2 and 8 patch ids")
    by_id = {r["id"]: r for r in catalogue.load_ranked(_conn())}
    missing = [i for i in patch_ids if i not in by_id]
    if missing:
        raise ValueError(f"unknown ids: {missing}")
    rows = [by_id[i] for i in patch_ids]
    keys = ["grit", "weight", *[k for k in query.SORTABLE if k not in ("grit", "weight")]]
    return {
        "patches": [{"id": r["id"], "name": r["name"], "library": r.get("library", ""), "category": r["category"],
                     "labels": r["labels"], "n_samples": r.get("n_samples", 1)} for r in rows],
        "same_category": len({r["category"] for r in rows}) == 1,
        "values": {k: [round(query._value(r, k), 3) for r in rows] for k in keys},
    }


UI_URL = os.environ.get("PATCH_EAR_UI_URL", "http://127.0.0.1:8765")


@mcp.tool()
def listen_link(patch_ids: list[str], title: str = "") -> dict:
    """A link to a page where the user can LISTEN to a shortlist (1-40 patch ids): one player per sound with its name, library,
    labels and mood tags, plus "play all". Give it whenever you propose specific sounds, so the user can audition them before
    deciding ("click to hear my proposal"). The page is served by the patch-ear web UI on the user's computer; if `ui_running`
    is false, tell the user to start it (`just serve` in the patch-ear folder) and give the link anyway."""
    if not 1 <= len(patch_ids) <= 40:
        raise ValueError("give between 1 and 40 patch ids")
    known = {r[0]: r[1] for r in _conn().execute("SELECT id, name FROM patches")}
    missing = [i for i in patch_ids if i not in known]
    if missing:
        raise ValueError(f"unknown ids: {missing}")
    params = [("id", i) for i in patch_ids] + ([("title", title)] if title else [])
    u = urlparse(UI_URL)
    try:
        with socket.create_connection((u.hostname, u.port or 80), timeout=0.5):
            running = True
    except OSError:
        running = False
    return {"url": f"{UI_URL.rstrip('/')}/listen?{urlencode(params)}", "ui_running": running,
            "sounds": [known[i] for i in patch_ids],
            "note": None if running else "The patch-ear web UI is not running: start it with `just serve` in the patch-ear folder."}


@mcp.tool()
def save_note(patch_id: str, kind: str = "note", text: str = "", album: str = "", slot: str = "") -> dict:
    """Remember something the user said about a sound. Call it when the user states a verdict or a choice; don't ask first.
    kind: "note" (free text), "like", "reject", "pick" (their choice for an album slot: needs album, e.g. "city", and slot, e.g.
    "hard bass", "soft bass", "hard drum machine", "soft drum machine"; the newest pick per slot is current) or
    "tag_ok" / "tag_wrong" (their EAR says a mood/use tag shown on this sound is right or wrong; put the tag in text, e.g. "calm").
    Stored in notes.db, separate from the catalogue, so it survives re-analysis. Later chats can read it back with list_notes / get_palette."""
    if patch_id not in {r[0] for r in _conn().execute("SELECT id FROM patches WHERE id=?", (patch_id,))}:
        raise ValueError(f"unknown id '{patch_id}'")
    return notes.add(notes.connect(), patch_id, kind, text, album, slot)


@mcp.tool()
def list_notes(patch_id: str = "", album: str = "", kind: str = "", limit: int = 50) -> dict:
    """The user's saved notes, newest first, optionally for one sound, one album or one kind. Also returns how the user's ear verdicts
    split per tag (tag_feedback: {tag: {ok, wrong}}), i.e. how far the mood/use tags can be trusted. Check this at the start of
    album work and before leaning on a mood tag."""
    conn = notes.connect()
    names = {r[0]: r[1] for r in _conn().execute("SELECT id, name FROM patches")}
    rows = notes.listing(conn, patch_id, album, kind, limit)
    for r in rows:
        r["name"] = names.get(r["patch_id"], r["patch_id"])
    return {"notes": rows, "tag_feedback": notes.tag_feedback(conn)}


@mcp.tool()
def get_palette(album: str) -> dict:
    """The user's current picks for an album (one per slot, e.g. hard bass, soft bass, hard drum machine, soft drum machine), each with
    the sound's name and id. Read it first whenever the user talks about an album they have already started."""
    names = {r[0]: r[1] for r in _conn().execute("SELECT id, name FROM patches")}
    picks = notes.palette(notes.connect(), album)
    return {"album": album, "picks": [{"slot": p["slot"], "patch_id": p["patch_id"], "name": names.get(p["patch_id"], p["patch_id"]),
                                         "note": p["text"]} for p in picks]}


def _with_scores(conn, hits: list[tuple[str, float]]) -> list[dict]:
    by_id = {r["id"]: r for r in catalogue.load_ranked(conn)}
    return [query.summarize(by_id[i]) | {"similarity": round(s, 3)} for i, s in hits if i in by_id]


@mcp.tool()
def sounds_like(patch_id: str, n: int = 10, category: str | None = None) -> list[dict]:
    """Patches that SOUND closest to the given one, by a learned audio embedding (CLAP) rather than by the hand-built
    measurements: good for overall timbre, genre and feel ("something like this but in another library"). Works for every
    embedded category (Bass and Drum so far; check what is embedded by trying it with `category`).
    similar_to compares the measured features instead. Only presets that have been embedded are searched; similarity is cosine (1 = identical)."""
    from . import embed

    conn = _conn()
    return _with_scores(conn, embed.neighbours(conn, patch_id, n, category))


@mcp.tool()
def search_by_description(text: str, category: str | None = None, n: int = 10) -> list[dict]:
    """Rank embedded patches against a free-text description ("a wobbly dubstep bass", "a glassy bell pad").
    Best for broad character (genre, mood, instrument type). It is weaker than search_patches on narrow properties it
    can't hear precisely (roughness) and was only checked on Bass; use weight/grit sorting there. The first call loads the
    text model (several seconds); this needs the optional `clap` extra, otherwise it says so."""
    from . import embed

    conn = _conn()
    return _with_scores(conn, embed.search(conn, text, category, n))


def main() -> None:
    try:
        mcp.run()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
