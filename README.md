# Velixar CLI

Memory API for AI applications — from your terminal.

## Install

```bash
pip install velixar-cli
```

## Setup

```bash
velixar auth login
# paste your API key (vlx_...)
```

Or set the environment variable:
```bash
export VELIXAR_API_KEY="vlx_your_key"
```

## Usage

```bash
# Store a memory
velixar store "User prefers dark mode" --tier 0

# Search memories
velixar search "user preferences"

# List recent memories
velixar list --limit 20

# Get a specific memory
velixar get mem_abc123

# Delete a memory
velixar delete mem_abc123

# Check API health
velixar health

# Interactive shell
velixar interactive
```

## Interactive Mode

```
$ velixar interactive
╭─ Velixar Interactive Shell ─╮
│ Commands: store search list get delete quit │
╰──────────────────────────────╯

velixar> store User likes Python and Rust
✓ Stored: mem_xyz789

velixar> search programming languages
  mem_xyz789.. (0.923) User likes Python and Rust

velixar> quit
```

## Links

- [Documentation](https://docs.velixarai.com)
- [API Reference](https://docs.velixarai.com/api-reference/introduction)
- [Dashboard](https://velixarai.com)

### VOU count in your terminal

Run `velixar vou count` for the authoritative workspace count, or `velixar vou count --watch` to refresh every 10 seconds. `--interval` accepts 2–3600 seconds. Use `--since` / `--until` for server-defined time bounds and `--format json` for dashboard/status consumers (one JSON object per refresh). Existing `velixar vou summary` gives the detailed breakdown.

These commands use the configured API key and require `usage:read` plus the existing VOU beta access. They only read `/v1/vou/summary`; they do not invoke models. The display includes workspace, pending normalization, meter gaps and the server's window/coverage. Unknown measurements remain Unknown, while measured zero remains 0. Refresh failures exit with unavailable status rather than reprinting a stale count. VOU beta weights are assumptions, separate from inference tokens, and **not for billing**. A successful local CLI test does not prove deployed summary availability.
