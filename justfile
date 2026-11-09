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
