# patch-ear. Config is data/patch-ear.toml (NAS paths); `local` uses data/patch-ear-local.toml (the copy in the shared cache).

set shell := ["bash", "-uc"]

default:
    @just --list

[doc('Web UI on http://127.0.0.1:8765 (config: data/patch-ear.toml)')]
serve port="8765":
    uv run patch-ear serve --port {{port}}

[doc('Web UI using the local library copy (data/patch-ear-local.toml)')]
serve-local port="8765":
    uv run patch-ear -c data/patch-ear-local.toml serve --port {{port}}

[doc('Measure sources and update the catalogue (extra args -> patch-ear analyze, e.g. --source X --force)')]
analyze *args:
    uv run --extra press patch-ear analyze {{args}}

[doc('Embed audition files with CLAP (experimental; category optional)')]
embed *args:
    uv run --extra clap patch-ear embed {{args}}

# Claude Desktop starts with a minimal PATH, so find uv here. stdout is the MCP channel: nothing may print to it (the @ hides the echo).
[doc('MCP server on stdio (what Claude Desktop launches; includes the clap extra for search_by_description)')]
mcp:
    @PATH="$HOME/.local/bin:$PATH" uv run --extra clap patch-ear-mcp

# Trained mood + perceptual timbre scores (Essentia, AudioCommons timbral_models). Separate Python 3.12 env: essentia-tensorflow has no 3.14 wheels.
[doc('One-time: build the 3.12 sidecar env (sidecar/.venv). Models are in data/essentia-models')]
sidecar-setup:
    cd sidecar && uv venv --python 3.12 .venv
    cd sidecar && uv pip install --python .venv/bin/python essentia-tensorflow scikit-learn scipy soundfile librosa pyloudnorm numpy
    cd sidecar && uv pip install --python .venv/bin/python --no-deps timbral_models

[doc('Score every catalogue sound with the sidecar (extra args: --limit N, --category Kick, --no-timbre, --force)')]
descriptors *args:
    PATCH_EAR_HOME=$PWD/data sidecar/.venv/bin/python sidecar/descriptors.py {{args}}
