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
- **State lives in one folder, the patch-ear home** (`config.home()`: `$PATCH_EAR_HOME`, else `./data`, gitignored): `patch-ear.toml`,
  `catalogue.db`, `previews/`, subset files. Relative paths in the config resolve from it. Library folders are absolute paths (NAS mounts);
  nothing refers to sibling repos. Cloud: mount a volume at the home folder and the library as a read-only mount. `-c`/`PATCH_EAR_CONFIG` override the file.
- `library` type (auto-detect) still exists for quick exploration of a raw folder: under "Kits"/"... Kit" -> the folder below is ONE kit
  (averaged over <=24 hits, category Drum); a folder whose names carry notes (>=60%) -> ONE multisample; else every file is a one-shot.
  It filters nothing and misreads unusual naming, so the shipped config does NOT use it for the synth packs (see below). `library` = the
  source folder's name unless the source sets `library`.
- Shape is DECLARED BY HAND per source, not guessed: vendors name files too differently for `library` to cover (it kept needing regexes).
  Use `multisample` (folder = preset), `filename` (several patches in one folder, told apart by the name before the note; `_split_note`
  handles `-048_c2`, ` C3`, `_C2_0002` round-robin takes, `-AB12` export tags, a leading MIDI number), `oneshots`, `kit`. `exclude = [...]`
  (folder names / fnmatch, relative to the source) leaves out loops, FX, phrases: only what ships as a preset is scanned (see prepare-sd-cards
  `_SFM_SKIP`). Category comes from path words unless the source sets one. Kit previews are the measured hits concatenated (`_kit_preview`).
- Explicit types mirror patch-press library shapes: multisample (folder = preset), filename (`<patch>-<midi>_<note>` in one folder),
  oneshots, drumkit, kit, wavetable; plus bitwig/clap/vst3 (config stored, analyser not built). Old `group=` configs are mapped.
  NO source type is device-specific (the old `deluge`/`bento` types were removed, #16).
- `press` type (press.py; needs `uv sync --extra press`) is for what only patch-press can read (CLAP/VST3 plugin presets rendered one note, Kontakt/SFZ/Bitwig maps): it walks a patch-press Configs tree
  (`<root>/<library>/<subfolders>/<preset>.yaml`; prepare-sd-cards/output/Configs); the first folder is the `library`, id =
  `<source>:<config path without .yaml>`. `only_list` is an optional SUBSET file (config paths, `*`/`?` allowed, `#` comments;
  see `subset.txt`); unmatched patterns are reported as notes. Kind (kit / wavetable / instrument) is read from the config
  (`profile: drums`, `source.drumkit`/`files`, `source.type: wavetable`), not from a device layout; granular configs are skipped with a note.
  Audio comes from patch-press's OWN adapters (library, Kontakt/SFZ maps, Bitwig, wavetable) and plugin presets are RENDERED one note
  (render_note, 4 s hold; re-rendered at a corrected key when the preset sounds whole octaves away from the key). Categories/tags from
  patch-press `derive_tags` (a 15-tag vocabulary: Bass, Pad, Drum, ...; "Synth" is generic so the more specific tag wins, all tags kept).
  Previews of rendered audio go to previews/ next to the catalogue. Change detection stats the config yaml only (library edits need
  --force). Run with `uv run --extra press patch-ear analyze`.
- `data/patch-ear.toml` (gitignored) was reverse-engineered from the Bento/Deluge shipping lists: 45 `library`/`kit` sources over the NAS
  (kits, synth multisamples, Rastsound, Wave Alchemy, Just Add Drums) + one `press` source (`data/plugins.txt`: Diva/Zebra2/ZebraHZ).
  Dry run (no audio): ~4,900 presets. `essential_wav_24bit` Synths are NOT listed (duplicates of the standalone libs); its Drums/02. Kits IS
  (holds kits the standalone folders lack). Not covered yet: wavetables (Liam, Polyend), granular, Bitwig .multisample (Orchestral Tools, Electric Keys).
  Name detection also handles `<patch>_C2`, `<patch>-24` (MIDI number) and several presets in one folder (grouped by the shared base name).
  When a source is pointed AT a "Kits" folder, that folder counts as the Kits level. Known rough spot: some `essential-kits` machine folders
  become one kit when their sub-kits aren't named "...Kit".

- **Shared render cache.** The `press` source's plugin renders go through patch-press's render cache, so a preset is rendered once across patch-ear and prepare-sd-cards builds. Set the location with the optional `[cache] dir = "..."` in `patch-ear.toml` (applied at config load; an explicit `PATCH_PRESS_CACHE_DIR` wins; it survives UI saves). HOLD_S/TOTAL_S/velocity come from `patch_press.cache.reference`, which the build also renders, so don't hard-code them here. The octave-corrected re-render is a different note and misses until something else renders it. WAV sources read files in place and don't use the cache.
- `analyze --limit N` only measures the first N presets of each source; it prunes against the FULL list, so the rest of the catalogue is kept (it once wiped 145 plugin presets before this was fixed).

## Commands
- `uv sync`; `uv run patch-ear analyze [--source X] [--force]`; `uv run patch-ear search bass --sort grit --filter centroid_hz::400`
- MCP (Claude Desktop config): command `uv`, args `["--directory", "<abs path to patch-ear>", "run", "patch-ear-mcp"]`,
  env `PATCH_EAR_HOME=<abs path to the home folder>` (or `PATCH_EAR_CONFIG=<abs path to patch-ear.toml>`).

## Status / not built yet
- Only WAV types are analysable (`SourceType.analyzable`). Plugin (CLAP/VST3) presets should be rendered via patch-press adapters
  (patch-render) at a fixed reference note, then fed through `features.extract` — not implemented.
- multisample/filename types measure up to THREE samples per preset (`_pick_notes`: the reference note, an octave below and above; features are
  the mean, the audition is the reference sample; `representative`=3). Matched on the key SENT, not the sounding pitch (a patch with oscillators
  an octave up really is thin at that key): the leading MIDI number in the name if it agrees with the note (`57_Bass_A2` -> 57), else the note
  name with C4=60. Libraries with only a note name may use the other octave convention (C3=60): then their notes are an octave off (no
  per-source offset yet). Still a heuristic, not a patch-level measure.
- UI was checked headless (Firefox via WebDriver BiDi: console clean, progress/dialog/picker rendered) but not by hand in a normal browser.
- FIXED (2026-11-02): frame-level stats (centroid/rolloff/flatness/zcr) were an unweighted mean over frames, so the silent tail of a
  decaying note dominated (a clean sub measured 932 Hz centroid / "noisy"). Now loudness-weighted, centroid/rolloff from the power spectrum.
- VALIDATED against the user's ears (2026-11-02, 12 bass patches, Spearman +0.78): grit = flatness + hi_ratio (>2 kHz). Crest factor was
  REMOVED from grit (a sine/triangle has low crest without distortion: Junos Sub Only ranked gritty). Candidates that did WORSE on those
  12: f0-relative harmonic ratios (+0.5). Small n: re-validate before trusting fine differences.
- Reference pitch: libraries label notes inconsistently (a "C1" is 33 Hz in Diva/Zebra renders, 65 Hz in Junos), so `press` presets are
  matched on MEASURED pitch (`features.measured_midi`, octave offset applied to the label scale). `ref_midi` = real pitch (45 = A2).
  Patches that only contain low samples (e.g. only C1) still get measured low: 10th pct of bass sample pitch is 38 Hz.
- DIMENSIONS (20 + f0_hz, see features.py docstring): time/dynamics (duration attack decay sustain_db rms crest), spectrum (centroid rolloff
  low_ratio hi_ratio flatness zcr), pitch/harmonics (pitch_clarity inharmonic_ratio harm_tilt_db), modulation (roughness = 20-150 Hz beating
  excluding the note's own periodicity; lfo_rate/depth = slow pulsing, needs >=3 cycles + a clear peak), evolution (timbre_movement), stereo.
  Added 2026-11-02; grit AND roughness have been checked against the user's ears (roughness 2026-11-04: extremes test on Bass,
  top 5 and bottom 5 both confirmed; mid-range ordering not checked). The others were sanity-checked on names (bells -> inharmonic,
  sine piano -> low harmonic richness, wavetable morphs -> movement) and are NOT ear-validated. Decay/sustain use a 100 ms smoothed envelope.
  Pitch-dependent measures are meaningless for unpitched sounds (inharmonic_ratio=1, harm_tilt=0 by convention); time-based measures are
  meaningless for single-cycle wavetables (rows carry a `type:wavetable` tag; labels skip them). Rows also get type:instrument|kit|wavetable.
- STILL OPEN: bright-but-regular waves (saw/square: "basic wave", e.g. Zebra Third FM Bass, Junos Sub Only) rank mid-grit because grit
  conflates brightness with noise; inharmonic_ratio now exists but grit does not use it yet (untested against verdicts). Attack-only distortion (Carnage Amp) is
  averaged away. (The old `pulsing 0.1Hz` false label is fixed: 59 patches pulse now.)
- weight VALIDATED on Bass (2026-11-05, extremes): heaviest 5 confirmed. Lightest were wrong until the representative note was fixed
  (a name without a MIDI number fell back to the alphabetical middle file = D#7; then A2 read as MIDI 45 when the library sent 57). Now plausible
  (dubstep/standup/electric bass light). The vendor's folder decides the category: "Bass/AlterEgo..." and "Bass & Guitar/Synth Guitar" are Bass.
- EMBEDDINGS (experimental, `embed.py`, `uv sync --extra clap`; CPU torch index in pyproject): LAION-CLAP vectors of each preset's audition file in the
  `embeddings` table. CLI: `embed [category]`, `text "phrase" [category]`, `probe [category]`. On Bass (n=276) the zero-shot text axes track
  weight +0.60, decay +0.61, grit +0.42, inharmonic +0.22, roughness +0.12: use features for narrow validated properties, embeddings for broad
  character. MCP tools: `sounds_like(patch_id)` (stored vectors, numpy only, instant) and `search_by_description(text)` (loads the CLAP text model on
  first call, several seconds; errors clearly if the extra is missing). Only embedded presets are searched (only Bass so far).
- Feature definitions (grit, weight) were checked only on Bass, at the extremes. Validate on patches the user knows
  before trusting rankings (see memory: verify before "confirmed").
- No automated tests (like the sibling repos); smoke check by analyzing a few known files.
