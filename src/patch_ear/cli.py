from __future__ import annotations

import argparse
import json

from . import catalogue, query
from .analyze import analyze
from .config import DEFAULT_CONFIG, load_config


def _run_analyze(cfg, only, force, limit=None) -> None:
    from tqdm import tqdm

    bar = None

    def on_event(kind, **kw):
        nonlocal bar
        if kind == "plan":
            bar = tqdm(total=kw["total"], unit="patch")
        elif kind == "source_skip":
            print(f"[{kw['source']}] skipped: {kw['reason']}")
        elif kind == "source_note":
            print(f"[{kw['source']}] note: {kw['note']}")
        elif kind == "item":
            bar.update(1)
            bar.set_description(kw["source"])
            if kw["state"] == "failed":
                tqdm.write(f"  FAIL {kw['message']}")
        elif kind == "source_end":
            tqdm.write(f"[{kw['source']}] analysed {kw['analysed']}, unchanged {kw['unchanged']}, failed {kw['failed']}, removed {kw['removed']}")

    analyze(cfg, only=only, force=force, on_event=on_event, limit=limit)
    if bar:
        bar.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="patch-ear")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="measure all sources and update the catalogue (incremental)")
    a.add_argument("--source", help="only this source name")
    a.add_argument("--force", action="store_true", help="re-analyse unchanged files")
    a.add_argument("--limit", type=int, help="only the first N presets of each source (smoke test)")

    s = sub.add_parser("search", help="query the catalogue from the terminal")
    s.add_argument("category", nargs="?", default=None)
    s.add_argument("--tag")
    s.add_argument("--sort", default="grit")
    s.add_argument("--asc", action="store_true")
    s.add_argument("--limit", type=int, default=15)
    s.add_argument("--filter", action="append", default=[], metavar="FEATURE:MIN:MAX", help="e.g. centroid_hz::400")

    w = sub.add_parser("serve", help="web UI: configure sources/plugins, run analysis with progress, browse the catalogue")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8765)

    args = ap.parse_args()
    if args.cmd == "serve":
        import uvicorn

        from .server import create_app

        print(f"patch-ear UI on http://{args.host}:{args.port}  (config: {args.config})")
        uvicorn.run(create_app(args.config), host=args.host, port=args.port, log_level="warning")
        return
    cfg = load_config(args.config)
    if args.cmd == "analyze":
        _run_analyze(cfg, args.source, args.force, args.limit)
    else:
        filters = {}
        for f in args.filter:
            key, lo, hi = f.split(":")
            filters[key] = [float(lo) if lo else None, float(hi) if hi else None]
        rows = catalogue.load_ranked(catalogue.connect(cfg.catalogue, readonly=True), args.category)
        for r in query.search(rows, filters, args.sort, not args.asc, args.limit, args.tag):
            print(json.dumps(query.summarize(r)))
