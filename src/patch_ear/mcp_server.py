"""MCP server: read-only queries over the catalogue. Does NO audio analysis."""

from __future__ import annotations

import os
import sys

from mcp.server.mcpserver import MCPServer

from . import catalogue, query
from .config import DEFAULT_CONFIG, load_config

mcp = MCPServer("patch-ear")


def _conn():
    cfg = load_config(os.environ.get("PATCH_EAR_CONFIG", DEFAULT_CONFIG))
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
                   sort_by: str = "grit", descending: bool = True, limit: int = 15) -> list[dict]:
    """Rank patches by measured audio character, within a category (percentiles are per category) and/or a tag.

    category: the primary category (the most specific tag, e.g. Bass, Pad, Drum, Atmosphere).
    tag: any of a patch's tags (a patch can have several, e.g. Synth + Lead).

    library: the library a preset comes from (case-insensitive substring, e.g. "drumtrax"); list_categories shows them.

    filters: {measurement: [min_or_null, max_or_null]}, e.g. {"centroid_hz": [null, 400], "side_mid_db": [null, -40]}.
    sort_by: grit, weight, or any raw measurement (centroid_hz, low_ratio, flatness, lfo_rate_hz, ...).
    Results come from measurements, not preset names. The user's ears make the final call."""
    rows = catalogue.load_ranked(_conn(), category)
    return [query.summarize(r) for r in query.search(rows, filters, sort_by, descending, limit, tag, library)]


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


def main() -> None:
    try:
        mcp.run()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
