"""Local web UI backend (Starlette). Binds to localhost; hosts/origins are checked because it can read the
filesystem listing and start jobs."""

from __future__ import annotations

import asyncio
import functools
import re
import threading
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlencode, urlparse

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import catalogue, origin, query
from .analyze import _MIDI_ANYWHERE, _NOTE_SUFFIX, analyze
from .config import SOURCE_TYPES, default_config, load_config, load_raw, parse, save_raw

MATCH_RMS_DB = -20.0  # audition target: average level, in dBFS
MATCH_PEAK_DB = -1.0  # never boost past this peak (no clipping)
MATCH_MAX_CUT_DB, MATCH_MAX_BOOST_DB = 12.0, 60.0


@functools.lru_cache(maxsize=128)
def _matched_wav(path: str, mtime_ns: int) -> bytes:
    """The file as 16-bit WAV with its average level brought to MATCH_RMS_DB (quiet sounds up, loud ones down a little), limited so the
    peak stays under MATCH_PEAK_DB. Only for listening: the catalogue measurements always come from the original file."""
    import io

    import numpy as np
    import soundfile as sf

    x, sr = sf.read(path, dtype="float32", always_2d=True)
    peak = float(np.abs(x).max())
    if peak < 1e-7:  # digital silence: nothing to match
        return Path(path).read_bytes()
    rms = float(np.sqrt((x.astype(np.float64) ** 2).mean()))
    gain_db = min(MATCH_RMS_DB - 20 * np.log10(rms + 1e-12), MATCH_PEAK_DB - 20 * np.log10(peak))
    gain_db = float(np.clip(gain_db, -MATCH_MAX_CUT_DB, MATCH_MAX_BOOST_DB))
    out = io.BytesIO()
    sf.write(out, np.clip(x * (10 ** (gain_db / 20)), -1.0, 1.0), sr, format="WAV", subtype="PCM_16")
    return out.getvalue()

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "testserver"}
NOTE_TOKEN = re.compile(r"^[a-g][#b]?-?\d$", re.IGNORECASE)


class Job:
    """One background analysis at a time; state is polled by the UI."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.state: dict = {"running": False}

    def snapshot(self) -> dict:
        with self.lock:
            s = dict(self.state)
            s["sources"] = {k: dict(v) for k, v in s.get("sources", {}).items()}
            s["log"] = list(s.get("log", []))
            return s

    def _log(self, line: str) -> None:
        log = self.state.setdefault("log", [])
        log.append(f"{time.strftime('%H:%M:%S')} {line}")
        del log[:-300]

    def on_event(self, kind: str, **kw) -> None:
        with self.lock:
            st = self.state
            if kind == "plan":
                st["total"] = kw["total"]
                st["sources"] = {n: {"total": t, "done": 0, "analysed": 0, "unchanged": 0, "failed": 0, "state": "queued"}
                                 for n, t in kw["sources"].items()}
                self._log(f"plan: {kw['total']} patches in {len(kw['sources'])} source(s)")
            elif kind == "source_skip":
                st.setdefault("skipped_sources", {})[kw["source"]] = kw["reason"]
                self._log(f"[{kw['source']}] skipped: {kw['reason']}")
            elif kind == "source_note":
                self._log(f"[{kw['source']}] note: {kw['note']}")
            elif kind == "source_start":
                st["sources"][kw["source"]]["state"] = "running"
                self._log(f"[{kw['source']}] started ({kw['total']} patches)")
            elif kind == "item":
                src = st["sources"][kw["source"]]
                src["done"] += 1
                src[kw["state"]] += 1
                st["done"] = st.get("done", 0) + 1
                st["current"] = f"{kw['source']}: {kw['name']}"
                if kw["state"] == "failed":
                    self._log(f"FAIL {kw['message']}")
            elif kind == "source_end":
                st["sources"][kw["source"]]["state"] = "done"
                st["sources"][kw["source"]]["removed"] = kw["removed"]
                self._log(f"[{kw['source']}] analysed {kw['analysed']}, unchanged {kw['unchanged']}, failed {kw['failed']}, removed {kw['removed']}")
            elif kind == "cancelled":
                st["cancelled"] = True
                self._log("cancelled")

    def start(self, cfg, only: str | None, force: bool) -> bool:
        with self.lock:
            if self.state.get("running"):
                return False
            self.cancel.clear()
            self.state = {"running": True, "started_at": time.time(), "done": 0, "total": 0, "only": only, "force": force, "log": []}

        def run() -> None:
            try:
                analyze(cfg, only=only, force=force, on_event=self.on_event, cancelled=self.cancel.is_set)
            except Exception as e:  # surface to the UI instead of dying silently
                with self.lock:
                    self.state["error"] = f"{type(e).__name__}: {e}"
                    self._log(f"ERROR {self.state['error']}")
            finally:
                with self.lock:
                    self.state["running"] = False
                    self.state["finished_at"] = time.time()

        threading.Thread(target=run, daemon=True).start()
        return True


def _count_wavs(root: Path, limit: int = 50000) -> tuple[int, int, list[str]]:
    n, folders, sample = 0, set(), []
    for p in root.rglob("*.wav"):
        if p.name.startswith("."):
            continue
        n += 1
        folders.add(p.parent)
        if len(sample) < 6:
            sample.append(str(p.relative_to(root)))
        if n >= limit:
            break
    return n, len(folders), sample


def suggest_type(root: Path) -> dict:
    """Best guess at the library shape. Like patch-press, this cannot be fully auto-detected: the user decides."""
    exts = {p.suffix.lower() for p in root.rglob("*") if p.is_file()} if sum(1 for _ in root.iterdir()) < 5000 else set()
    n, folders, sample = _count_wavs(root)
    info = {"wavs": n, "folders_with_wavs": folders, "sample": sample, "suggestion": None, "reason": ""}
    if ".multisample" in exts:
        return info | {"suggestion": "bitwig", "reason": "contains .multisample files"}
    if exts & {".h2p", ".clap-preset", ".fxp"}:
        return info | {"suggestion": "clap", "reason": "contains plugin preset files"}
    if n == 0:
        return info | {"reason": "no .wav files found"}
    stems = [p.stem for p in list(root.rglob("*.wav"))[:400] if not p.name.startswith(".")]
    suffixed = sum(bool(_NOTE_SUFFIX.match(s)) for s in stems) / len(stems)
    noted = sum(any(NOTE_TOKEN.match(t) for t in re.split(r"[ _\-.]+", s)) or bool(_MIDI_ANYWHERE.search(s)) for s in stems) / len(stems)
    direct = sum(1 for p in root.iterdir() if p.suffix.lower() == ".wav")
    avg = n / max(folders, 1)
    if suffixed >= 0.6 and direct > 0:
        return info | {"suggestion": "filename", "reason": f"{suffixed:.0%} of files look like <patch>-<midi>_<note> and sit directly in the folder"}
    if suffixed >= 0.6:
        return info | {"suggestion": "multisample", "reason": f"{suffixed:.0%} of files carry a note, in {folders} subfolder(s) (one folder = one preset; "
                       "use 'Multisample, flat folder' if a folder holds several patches)"}
    if noted >= 0.5 and folders >= 1:
        return info | {"suggestion": "multisample", "reason": f"{noted:.0%} of file names carry a note; {folders} folder(s) of ~{avg:.0f} files"}
    if folders >= 2 and avg >= 3:
        return info | {"suggestion": "drumkit", "reason": f"{folders} folders of ~{avg:.0f} un-pitched files (could also be 'kit' or 'oneshots')"}
    return info | {"suggestion": "oneshots", "reason": "un-pitched loose files"}


def create_app(config_path: str | Path | None = None) -> Starlette:
    cfg_path = Path(config_path or default_config()).expanduser().resolve()
    job = Job()
    if cfg_path.exists():
        load_config(cfg_path)  # applies [cache] dir (PATCH_PRESS_CACHE_DIR), which playback fetches through

    def cfg():
        return parse(load_raw(cfg_path), cfg_path.parent)

    def err(msg: str, code: int = 400) -> JSONResponse:
        return JSONResponse({"error": msg}, status_code=code)

    def db(readonly=True):
        c = cfg()
        return catalogue.connect(c.catalogue, readonly=True) if c.catalogue.exists() else None

    async def index(_: Request):
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    async def types(_: Request):
        return JSONResponse([asdict(t) for t in SOURCE_TYPES])

    async def get_config(_: Request):
        raw = load_raw(cfg_path)
        return JSONResponse({"path": str(cfg_path), "exists": cfg_path.exists(), "config": raw,
                             "resolved_catalogue": str(cfg().catalogue)})

    async def put_config(request: Request):
        try:
            norm = save_raw(cfg_path, await request.json())
        except (ValueError, KeyError, TypeError) as e:
            return err(f"invalid config: {e}")
        return JSONResponse({"config": norm})

    async def fs(request: Request):
        p = Path(request.query_params.get("path") or "~").expanduser()
        try:
            p = p.resolve()
            if not p.is_dir():
                p = p.parent
            entries = []
            for c in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                if c.name.startswith(".") or not c.is_dir():
                    continue
                entries.append({"name": c.name, "path": str(c)})
            wavs = sum(1 for c in p.iterdir() if c.suffix.lower() == ".wav")
            files = [{"name": c.name, "path": str(c)} for c in sorted(p.iterdir()) if c.is_file() and c.suffix.lower() == ".clap"]
        except (OSError, PermissionError) as e:
            return err(str(e))
        return JSONResponse({"path": str(p), "parent": str(p.parent) if p.parent != p else None,
                             "dirs": entries[:1000], "files": files, "wavs_here": wavs, "home": str(Path.home())})

    async def inspect(request: Request):
        p = Path(request.query_params.get("path", "")).expanduser()
        if not p.is_dir():
            return err(f"not a folder: {p}")
        return JSONResponse(suggest_type(p))

    async def start_analyze(request: Request):
        body = await request.json()
        try:
            c = cfg()
        except ValueError as e:
            return err(str(e))
        if not job.start(c, body.get("source") or None, bool(body.get("force"))):
            return err("an analysis is already running", 409)
        return JSONResponse({"started": True})

    async def cancel(_: Request):
        job.cancel.set()
        return JSONResponse({"ok": True})

    async def status(_: Request):
        snap = job.snapshot()
        counts = {}
        conn = db()
        if conn:
            counts = {r["source"]: r["n"] for r in conn.execute("SELECT source, COUNT(*) n FROM patches GROUP BY source")}
            conn.close()
        return JSONResponse({"job": snap, "catalogued": counts})

    async def categories(_: Request):
        conn = db()
        out, sources = {}, {}
        if conn:
            out = {r["category"]: r["n"] for r in conn.execute("SELECT category, COUNT(*) n FROM patches GROUP BY category")}
            sources = {r["source"]: r["n"] for r in conn.execute("SELECT source, COUNT(*) n FROM patches GROUP BY source")}
            conn.close()
        return JSONResponse({"categories": out, "sources": sources, "sortable": query.SORTABLE})

    async def browse(request: Request):
        q = request.query_params
        conn = db()
        if not conn:
            return JSONResponse({"rows": [], "total": 0})
        rows = catalogue.load_ranked(conn, q.get("category") or None)
        conn.close()
        if q.get("tag"):
            rows = [r for r in rows if q["tag"].lower() in (t.lower() for t in r["tags"])]
        terms = (q.get("q") or "").lower().split()  # every word must match somewhere: "diva bass", "808 kit", "pad vocal"
        if terms:
            def hay(r):
                return " ".join([r["name"], r["source"], r["category"], *r["tags"], *r["labels"]]).lower()
            rows = [r for r in rows if all(t in hay(r) for t in terms)]
        sort = q.get("sort", "grit")
        if sort not in query.SORTABLE:
            return err(f"unknown sort '{sort}'")
        rows.sort(key=lambda r: query._value(r, sort), reverse=q.get("dir", "desc") == "desc")
        limit = max(1, min(int(q.get("limit", 100)), 500))
        return JSONResponse({"total": len(rows), "rows": [query.summarize(r, full=True) for r in rows[:limit]]})

    async def audio(request: Request):
        conn = db()
        row = conn.execute("SELECT path FROM patches WHERE id=?", (request.query_params.get("id", ""),)).fetchone() if conn else None
        if not row:
            return err("not found", 404)
        try:  # the catalogue holds the origin (NAS file / zip member); playing it fetches it through the shared cache
            path = await asyncio.to_thread(origin.local, row["path"])
        except (OSError, KeyError, RuntimeError):
            return err("not found", 404)
        if not path.is_file():
            return err("not found", 404)
        if request.query_params.get("match") == "1":  # audition at a matched loudness; the file and its measurements are untouched
            try:
                return Response(_matched_wav(str(path), path.stat().st_mtime_ns), media_type="audio/wav",
                                headers={"Cache-Control": "no-store"})
            except Exception:  # unreadable by soundfile: fall back to the original
                pass
        return FileResponse(path, media_type="audio/wav")

    async def listen(request: Request):
        """A small page that plays a shortlist: /listen?id=<patch id>&id=<patch id>...&title=... (what the MCP `listen_link` returns)."""
        import html

        ids = request.query_params.getlist("id")[:40]
        conn = db()
        by_id = {r["id"]: r for r in catalogue.load_ranked(conn)} if conn and ids else {}
        title = html.escape(request.query_params.get("title", "") or "Listen")
        items = []
        for i in ids:
            r = by_id.get(i)
            if not r:
                items.append(f'<li class="missing">{html.escape(i)} (not in the catalogue)</li>')
                continue
            q = urlencode({"id": i})
            meta = " · ".join(x for x in [r.get("library", ""), r["category"], ", ".join(r["labels"][:4]),
                                            ("mood: " + ", ".join(r.get("mood", []))) if r.get("mood") else "",
                                            ("use: " + ", ".join(r.get("use", []))) if r.get("use") else ""] if x)
            items.append(f'<li><div class="n">{html.escape(r["name"])}</div><div class="m">{html.escape(meta)}</div>'
                         f'<audio controls preload="none" data-src="/api/audio?{q}" src="/api/audio?{q}&amp;match=1"></audio></li>')
        body = "".join(items) or "<li class='missing'>No sounds given.</li>"
        page = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>:root{{--bg:#fff;--fg:#1b1b1b;--mut:#666;--card:#f3f3f3}}@media(prefers-color-scheme:dark){{:root{{--bg:#161616;--fg:#eee;--mut:#9a9a9a;--card:#232323}}}}
body{{background:var(--bg);color:var(--fg);font:16px system-ui,sans-serif;margin:0;padding:16px;max-width:720px}}
h1{{font-size:1.2rem}}ol{{padding:0;list-style:none}}li{{background:var(--card);border-radius:8px;padding:12px;margin:10px 0}}
.n{{font-weight:600}}.m{{color:var(--mut);font-size:.85rem;margin:4px 0 8px}}audio{{width:100%}}.missing{{color:var(--mut)}}button{{font:inherit;padding:8px 14px}}</style>
<h1>{title}</h1><p><button id="all">Play all in order</button>
<label style="margin-left:14px"><input type="checkbox" id="match" checked> Match loudness (quiet sounds are boosted so they can be judged)</label></p><ol>{body}</ol>
<script>const a=[...document.querySelectorAll('audio')];
document.getElementById('match').onchange=e=>{{window.__all=false;a.forEach(x=>{{x.pause();x.src=x.dataset.src+(e.target.checked?'&match=1':'')}})}};a.forEach((x,i)=>x.addEventListener('ended',()=>{{if(window.__all&&a[i+1])a[i+1].play()}}));
document.getElementById('all').onclick=()=>{{a.forEach(x=>x.pause());window.__all=true;if(a[0])a[0].play()}};
a.forEach(x=>x.addEventListener('play',()=>a.forEach(y=>{{if(y!==x)y.pause()}})));</script>"""
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    class OriginCheck:
        """Reject cross-site writes: browsers send Origin on POST/PUT; it must be a local host."""

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD"):
                origin = dict(scope["headers"]).get(b"origin", b"").decode()
                if origin and urlparse(origin).hostname not in {"localhost", "127.0.0.1", "::1"}:
                    await JSONResponse({"error": "bad origin"}, status_code=403)(scope, receive, send)
                    return
            await self.app(scope, receive, send)

    return Starlette(
        routes=[
            Route("/", index), Route("/api/types", types),
            Route("/api/config", get_config, methods=["GET"]), Route("/api/config", put_config, methods=["PUT"]),
            Route("/api/fs", fs), Route("/api/inspect", inspect),
            Route("/api/analyze", start_analyze, methods=["POST"]), Route("/api/cancel", cancel, methods=["POST"]),
            Route("/api/status", status), Route("/api/categories", categories),
            Route("/api/catalogue", browse), Route("/api/audio", audio), Route("/listen", listen),
        ],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=sorted(LOCAL_HOSTS)), Middleware(OriginCheck)],
    )
