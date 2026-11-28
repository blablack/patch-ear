# patch-ear. Config is data/patch-ear.toml. Sources name their origins (NAS folders, zips); audio is read through the shared
# patch-press cache, which is why every recipe that reads audio runs with the press extra (patch_ear.origin).
# Every recipe asks for BOTH extras: `uv run --extra X` syncs the venv to exactly X, so recipes asking for different
# extras would uninstall each other's (torch included) on every switch. The MCP launch configs do the same.

set shell := ["bash", "-uc"]

default:
    @just --list

[doc('Web UI on http://127.0.0.1:8765 (config: data/patch-ear.toml)')]
serve port="8765":
    uv run --extra clap --extra press patch-ear serve --port {{port}}

[doc('Measure sources and update the catalogue (extra args -> patch-ear analyze, e.g. --source X --force)')]
analyze *args:
    uv run --extra clap --extra press patch-ear analyze {{args}}

[doc('Embed audition files with CLAP (experimental; category optional)')]
embed *args:
    uv run --extra clap --extra press patch-ear embed {{args}}

[doc('Mood and use-case tags from the CLAP embeddings (run after every embed)')]
moods *args:
    uv run --extra clap --extra press patch-ear moods {{args}}

# Claude Desktop starts with a minimal PATH, so find uv here. stdout is the MCP channel: nothing may print to it (the @ hides the echo).
[doc('MCP server on stdio (what Claude Desktop launches; includes the clap extra for search_by_description)')]
mcp:
    @PATH="$HOME/.local/bin:$PATH" uv run --extra clap --extra press patch-ear-mcp

# Trained mood + perceptual timbre scores (Essentia, AudioCommons timbral_models). Separate Python 3.12 env: essentia-tensorflow has no 3.14 wheels.
[doc('One-time: build the 3.12 sidecar env (sidecar/.venv). Models are in data/essentia-models')]
sidecar-setup:
    cd sidecar && uv venv --python 3.12 .venv
    cd sidecar && uv pip install --python .venv/bin/python essentia-tensorflow scikit-learn scipy soundfile librosa pyloudnorm numpy
    cd sidecar && uv pip install --python .venv/bin/python --no-deps timbral_models

[doc('Score every catalogue sound with the sidecar (extra args: --limit N, --category Kick, --no-timbre, --force)')]
descriptors *args:
    PATCH_EAR_HOME=$PWD/data sidecar/.venv/bin/python sidecar/descriptors.py {{args}}
