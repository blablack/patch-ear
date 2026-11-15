# CLAUDE.md

Part of the patch-press workspace (see `../CLAUDE.md`). patch-ear measures the *character* of sounds and
serves it to Claude Desktop, which cannot hear audio and was choosing presets from names alone.

**Open work is tracked as GitHub issues, in two places:**
- **New libraries to add for the Claude Project, and project follow-ups** (restart/paste instructions, libraries considered and skipped): `blablack/studio-claude-project` (private; labels `library`, `user-action`, `reference`). Run `gh issue list` from `/home/blablack/src/studio-claude-project`.
- **Changes to patch-ear itself** (features, scoring and tag quality, tools, parked ideas): `blablack/patch-ear` (this repo, **public**: keep issue text generic, no personal details). Run `gh issue list` here.
Read them before starting; comment on or close an issue when its work is done or postponed. There is no TODO.md any more.

## Where the owner's configuration lives
The owner keeps their `patch-ear.toml` (library paths and choices), `patch-ear-local.toml`, `plugins.txt` and `subset.txt` in a separate private repo, `studio-claude-project/patch-ear/`, and `data/` holds **symlinks** to them (`data/patch-ear.toml` and the others). Code finds them as usual (`$PATCH_EAR_HOME/patch-ear.toml`). Rules that make this work: the catalogue path in the toml is absolute, and relative paths such as `only_list` resolve from the config file's real folder (config.py uses `path.resolve().parent`). Edit the config in the private repo; a copy of the pre-move files is in `data/pre-move-backup/`. This repo ships only `patch-ear.example.toml`.

## Working rules (set by the user, 2026-11-14)
- **Ask before processing any library.** Before adding or analysing ANY library or folder (a new `[[source]]`, a re-analysis, scoring a batch), say which one, how many sounds, and roughly how long it takes, then wait for a yes. One library at a time, never "everything on the list".
- **No loose one-shot percussion.** Drums are wanted only as pre-made kits (Samples From Mars "Kits" folders, Just Add Drums). Loops, textures, field recordings, FX and melodic/pad material are fine. Quantum Force (Ghosthack) is wanted for its loops only.
- **Adding one library, in order:** add the `[[source]]` to `data/patch-ear.toml`, copy `data/catalogue.db` to a `.bak-pre-<name>` file, then `just analyze --source <name>` (a plain `just analyze` re-checks every sound and is slow over the NAS), `just embed`, `uv run --extra clap patch-ear moods`, then `just descriptors --shard K/3` in three processes (new sounds only). Check that no sound is left unscored or without mood tags.
- `oneshots` sources (field recordings, FX, foley) give each file its own row in the source's `category`, with `folder:<name>` tags from its folders (for example `folder:outdoors`, `folder:tube`). Used for Found Sounds From Mars (category FX) and Polyend Fractured (category Percussion; the `Wood` family is the user's favourite for lofi snares).
- Loop roles come from folder/file words (`_LOOP_ROLES` in `analyze.py`); `texture` matches texture, backdrop, fragments. Check a new keyword against existing rows before adding it (an overly broad one misfiled Fractured as textures once).

## Architecture: two separate pieces
- **Analyzer** (`analyze.py`, `features.py`): offline batch. Walks `[[source]]`s from the TOML config, measures each
  sound, writes SQLite (`catalogue.py`). Incremental by mtime+size. Slow; run on library change.
- **MCP server** (`mcp_server.py`): live; reads the catalogue `mode=ro` and never touches audio. The ONLY thing it writes is the user's notes (`notes.py`, `notes.db` in the patch-ear home, separate so re-analysis cannot wipe it). Tool calls
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
  (folder names or file names / fnmatch, case-insensitive, relative to the source; `Bongo*` drops loose files from a flat folder) leaves out loops, FX, phrases: only what ships as a preset is scanned (see prepare-sd-cards
  `_SFM_SKIP`). Category comes from path words unless the source sets one. Kit previews are the measured hits concatenated (`_kit_preview`).
- Explicit types mirror patch-press library shapes: multisample (folder = preset), filename (`<patch>-<midi>_<note>` in one folder),
  oneshots, drumkit, kit, wavetable; plus bitwig/clap/vst3 (config stored, analyser not built). Old `group=` configs are mapped.
  NO source type is device-specific (the old `deluge`/`bento` types were removed, #16).
- `machine` type (analyze.py `_machine_items`): drum machines from their PRE-MADE KITS only (the user's choice: the individual-hit folders are
  deliberately not used). Point the source at a pack's Kits folder (varies: `02. Kits`, `Kits`, `One Shots/Kits`, `Drums/Kits`...). Every folder
  that directly holds hits is one kit. Per kit: a whole-kit row (category Drum, `type:kit`, averaged over <=24 hits) and one row per role present
  (Kick, Snare, Clap, Hat, Tom, Cymbal; category = role, tags `type:machine-role`, `role:x`, `kit`), so grit/weight are percentiles among kicks,
  among snares... Role comes from FILE-name words only ("bass" means kick here); percussion is ignored. Names are "<Pack> / <Kit> [/ <Role>]".
  Nested kit folders (606 `606/01. Clean Kit`) use the last two path parts. Packs with no Kits folder (Dr Bohm) are not covered. NOT ear-validated
  on drums yet. The whole-kit Drum rows average every hit incl. hats, so they do not characterise a machine: use the role rows.
  `essential_wav_24bit` vanished from the NAS (Nov 2026) and its source was dropped; its drum kits all exist in the full packs.
- `loops` type: every WAV is one row, category Loop, tags `type:loop` + `role:<drums|bass|guitar|melodic|drone|texture|fx|synth|loop>` from folder/file words
  AND the library name (`_loop_role`; "bass drum" counts as drums) plus `folder:<dashed path parts>` tags (`list_categories` hides `folder:` tags for category Loop: there are
  hundreds; they work as `tag=`). Repeated stems get "(parent folder)" in the name. NO tempo/key/bars on purpose: the user syncs loops in Bitwig/Deluge/Bento.
  Sources: Arhythmia (not Construction Kits/One Shots), the Samples From Mars loop folders (`loops_*`) and Yurt Rock (`loops_yr_*`, extracted from the zips into
  `~/ZGarbage/yurt-rock/<zip>/`; 13.7k loops, about 80% of category Loop, so Loop percentiles are mostly against Yurt Rock; per-library filtering does not rebase them).
  Yurt Rock drum packs also have `machine_yr_*` sources for the pre-made `<Session>_Kit` folders (103 kits; role words are split from camelCase, e.g. `HiHatClosed`).
  Not done: the Joey Waronker Collection (about 2.8k multi-velocity hits) and the loose one-shot/multitrack folders.
- MOODS (mood.py, `patch-ear moods`, needs --extra clap, run after `embed`): zero-shot CLAP scoring of every embedded sound against a fixed
  vocabulary (24 moods, 10 use-cases: MOODS/USES in mood.py), z-scored WITHIN the category, tags kept at z>=1 (max 4 per group), stored in
  the `moods` table. `search_patches(mood=..., use=...)` filters on them and rows show `mood`/`use`. NOT ear-validated: same caveat as all CLAP use.
  Re-run `moods` after any `embed`; edit the vocabulary in mood.py (prompts are plain sentences).
- LISTENING: MCP `listen_link(ids, title)` returns a URL to the web UI's `/listen` page (server.py `listen`): one audio player per sound with name,
  library, labels, mood/use tags, and "play all". The user clicks it from Claude Desktop. Needs `patch-ear serve` running (default http://127.0.0.1:8765,
  override with `PATCH_EAR_UI_URL`); the tool reports `ui_running` by probing the port. Localhost only; the MCP itself only exists in Claude Desktop, so that is where the link is used.
  Playback is **loudness-matched** by default (`/api/audio?id=...&match=1`, `_matched_wav` in server.py: average level to -20 dBFS, peak kept under -1 dBFS,
  boost up to 60 dB, cut up to 12 dB, 16-bit WAV); the `/listen` page has a checkbox for the original level and the Catalogue tab plays matched too. Added because
  very quiet previews (about 280 of 8,600 have an average level below -35 dB, for example a slow-attack ZebraHZ bass) were inaudible and unfair to judge. Measurements always come from the original file.
- NOTES (notes.py, MCP `save_note` / `list_notes` / `get_palette`): the user's notes, likes, rejects, album PICKS (album + slot such as "hard bass";
  the newest pick per slot is current) and EAR VERDICTS on mood/use tags (`tag_ok` / `tag_wrong`; `list_notes` returns per-tag counts, the way to
  validate the zero-shot tags against the user's ears). Lives in notes.db, not the catalogue.
- TRAINED SCORES (`sidecar/descriptors.py` + `descriptors.py` read side): Essentia Discogs-EffNet mood heads (aggressive, relaxed, sad, happy, party,
  acoustic, electronic, danceable, instrumental, tonal; probabilities), MSD-MusiCNN emomusic arousal/valence (1-9), and AudioCommons timbral_models
  (hardness, depth, brightness, roughness, warmth, sharpness, boominess, reverb). Written offline into the catalogue's `descriptors` table by a
  SEPARATE Python 3.12 env (essentia-tensorflow has no 3.14 wheels): `just sidecar-setup` once, then `just descriptors` (`--shard K/N` to run several
  workers, `--category`, `--limit`, `--force`; models live in data/essentia-models). Timbre only on the first 8 s and not on whole-kit (Drum) rows; processing
  order is by category priority. Query names: arousal, valence, mood_<x>, timbre_<x> (usable in sort_by/filters). Mood models were trained on music, so on
  single drum hits they saturate (every kick "sad", "electronic"): rank within a category. NOT ear-validated. The sidecar needs numpy-2 and
  librosa>=0.10 shims for timbral_models (see the file). Rerun `just descriptors` after new sounds are analysed.
- MCP `compare_patches(ids)`: side-by-side measurements for 2-8 patches.
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
  (holds kits the standalone folders lack). Not covered yet: wavetables (Liam, Polyend), granular, Bitwig .multisample beyond the folders in the next bullet (Orchestral Tools).
  Name detection also handles `<patch>_C2`, `<patch>-24` (MIDI number) and several presets in one folder (grouped by the shared base name).
- **Bitwig `.multisample` files** are measured through patch-press: `patch-press scan-bitwig "<folder>" "<patch-ear>/data/bitwig-configs/<Library>"` writes one YAML per
  file (it reads the zip's `multisample.xml`: roots, zones, loops), then a `press` source points at `data/bitwig-configs` and measures ONE note per instrument
  (`ref_midi`; the audition file is that note). Several `press` sources can share the config root: each takes its configs from a subset file (`only_list`, patterns
  like `Bass and Guitar/Acoustic_Bass*`) and can set its own `category`, which beats patch-ear's tag rules for everything it selects (used to keep the acoustic
  basses out of Bass: `bitwig_played_bass` -> Played-Bass, `bitwig_guitar` -> Guitar, `bitwig_multisamples` -> Keys). The config tree is regenerable and not versioned.
  The patch-press adapter reads both XML layouts (older: `<sample>` under the root; newer: inside `<layer>`, `track="true"`). Done: Electric Keys (4), Bass and
  Guitar (11), Rare Organs and Keys (47, source `bitwig_organs`, category Organ). Chromatic Percussion (2, source `bitwig_mallets`, category Mallets). The three single multisamples (Vocoder World, Analog Waves, Anti-Loops) are skipped on purpose. The `bitwig` source type in the Settings page is still a stub; use `press`.
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
