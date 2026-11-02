# CLAUDE.md

Part of the patch-press workspace (see `../CLAUDE.md`). patch-ear measures the *character* of sounds and
serves it to Claude Desktop, which cannot hear audio and was choosing presets from names alone.

## Architecture: two separate pieces
- **Analyzer** (`analyze.py`, `features.py`): offline batch. Walks `[[source]]`s from the TOML config, measures each
  sound, writes SQLite (`catalogue.py`). Incremental by mtime+size. Slow; run on library change.
- **MCP server** (`mcp_server.py`): live but READ-ONLY. Opens the catalogue `mode=ro`, never touches audio. Tool calls
  must return in seconds. Percentiles/labels are computed at query time (per category) so they never go stale.
- Shared query layer: `query.py`. Config: `config.py` (`patch-ear.toml`, copy from `patch-ear.example.toml`).
- **Web UI** (`server.py` Starlette + `static/index.html`, no build step): `uv run patch-ear serve` -> http://127.0.0.1:8765.
  Tabs: Analysis (job progress bars/ETA/log, settings), Libraries, Plugins, Catalogue (sort/filter/audition).
  The UI edits the same TOML (comments in hand-edited files are NOT preserved on save). Analysis runs in a background thread
  and the UI polls `/api/status`. Localhost only: Host and Origin are checked, audio is served only for paths already in the catalogue.
- Source `type` mirrors patch-press library shapes: multisample (folder = preset), filename (`<patch>-<midi>_<note>` in one folder),
  oneshots, drumkit, kit (a kit = ONE row, averaged over <=24 hits), wavetable; `bento` = patch.xml folders (patch-press output) with
  categories from patchindex.xml tags ("Synth" is generic, so the more specific tag wins; all tags kept) and `only_list` to restrict it;
  plus bitwig/clap/vst3 (config stored, analyser not built). Old `group=` configs are mapped.
- `deluge` type (press.py; needs `uv sync --extra press`): reads patch-press scan configs (prepare-sd-cards/output/Configs) and
  matches the selection list (`lists/deluge_selection.txt` = DELUGE_PATCHES, regenerate with tools/export_deluge_list.py) to configs
  by the DelugeExporter identity (SYNTHS|KITS/<collection>/<subfolder>/<name>). Audio comes from patch-press's OWN adapters
  (library, Kontakt/SFZ maps, Bitwig, wavetable) and plugin presets are RENDERED one note (render_note, 4 s hold; re-rendered at a
  corrected key when the preset sounds whole octaves away from the key). Categories from patch-press `derive_tags` (same 15 Bento tags).
  Previews of rendered audio are written to previews/ next to the catalogue. 2230/2256 selected presets have a config; the 26 missing
  are Diva presets never scanned. Change detection stats the config yaml only (library edits need --force). ~6 min for all 2230.
  Cross-check: 120 Diva presets in both Bento and Deluge lists, measured by different routes: feature correlations +0.76..+0.99
  (decay +0.52, expected: Bento WAVs are trimmed/looped), grit percentile +0.83, same pitch octave 94%.
- `patch-ear.toml` (gitignored) points at prepare-sd-cards/output/Bento/UserPatches with `lists/bento_shipped.txt` = BENTO_PATCHES.
  Regenerate the list after editing bento.py: `cd ../prepare-sd-cards && uv run python ../patch-ear/tools/export_bento_list.py ../patch-ear/lists/bento_shipped.txt`.
  Shipped Diva/Zebra patches are already rendered WAVs there, so they are analysable without a plugin renderer.

## Commands
- `uv sync`; `uv run patch-ear analyze [--source X] [--force]`; `uv run patch-ear search bass --sort grit --filter centroid_hz::400`
- MCP (Claude Desktop config): command `uv`, args `["--directory", "<abs path to patch-ear>", "run", "patch-ear-mcp"]`,
  env `PATCH_EAR_CONFIG=<abs path to patch-ear.toml>`.

## Status / not built yet
- Only WAV types are analysable (`SourceType.analyzable`). Plugin (CLAP/VST3) presets should be rendered via patch-press adapters
  (patch-render) at a fixed reference note, then fed through `features.extract` — not implemented.
- multisample/filename types analyse ONE representative sample per preset (nearest `ref_midi`, else middle by name): a heuristic, not a patch-level measure.
- UI was checked headless (Firefox via WebDriver BiDi: console clean, progress/dialog/picker rendered) but not by hand in a normal browser.
- FIXED (2026-11-02): frame-level stats (centroid/rolloff/flatness/zcr) were an unweighted mean over frames, so the silent tail of a
  decaying note dominated (a clean sub measured 932 Hz centroid / "noisy"). Now loudness-weighted, centroid/rolloff from the power spectrum.
- VALIDATED against the user's ears (2026-11-02, 12 bass patches, Spearman +0.78): grit = flatness + hi_ratio (>2 kHz). Crest factor was
  REMOVED from grit (a sine/triangle has low crest without distortion: Junos Sub Only ranked gritty). Candidates that did WORSE on those
  12: f0-relative harmonic ratios (+0.5). Small n: re-validate before trusting fine differences.
- Reference pitch: libraries label notes inconsistently (a "C1" is 33 Hz in Diva/Zebra renders, 65 Hz in Junos), so `bento` presets are
  matched on MEASURED pitch (`features.measured_midi`, octave offset applied to the label scale). `ref_midi` for bento = real pitch (45 = A2).
  Patches that only contain low samples (e.g. only C1) still get measured low: 10th pct of bass sample pitch is 38 Hz.
- DIMENSIONS (20 + f0_hz, see features.py docstring): time/dynamics (duration attack decay sustain_db rms crest), spectrum (centroid rolloff
  low_ratio hi_ratio flatness zcr), pitch/harmonics (pitch_clarity inharmonic_ratio harm_tilt_db), modulation (roughness = 20-150 Hz beating
  excluding the note's own periodicity; lfo_rate/depth = slow pulsing, needs >=3 cycles + a clear peak), evolution (timbre_movement), stereo.
  Added 2026-11-02; ONLY grit has been checked against the user's ears. The others were sanity-checked on names (bells -> inharmonic,
  sine piano -> low harmonic richness, wavetable morphs -> movement) and are NOT ear-validated. Decay/sustain use a 100 ms smoothed envelope.
  Pitch-dependent measures are meaningless for unpitched sounds (inharmonic_ratio=1, harm_tilt=0 by convention); time-based measures are
  meaningless for single-cycle wavetables (rows carry a `type:wavetable` tag; labels skip them). Bento rows also get type:instrument|kit|granular.
- STILL OPEN: bright-but-regular waves (saw/square: "basic wave", e.g. Zebra Third FM Bass, Junos Sub Only) rank mid-grit because grit
  conflates brightness with noise; inharmonic_ratio now exists but grit does not use it yet (untested against verdicts). Attack-only distortion (Carnage Amp) is
  averaged away. (The old `pulsing 0.1Hz` false label is fixed: 59 patches pulse now.)
- Feature definitions (grit, weight) are UNVALIDATED against the user's ears. Validate on patches the user knows
  before trusting rankings (see memory: verify before "confirmed").
- No automated tests (like the sibling repos); smoke check by analyzing a few known files.
