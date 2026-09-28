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

## Foundry

`velixar foundry` is the CLI surface for Velixar's model gateway. The gateway is not
deployed at a default production URL, so configure it explicitly:

```bash
export VELIXAR_GATEWAY_URL="https://<gateway-host>"
export VELIXAR_API_KEY="<velixar-api-key>"
velixar foundry status
velixar foundry models
velixar foundry run --prompt "Review this patch"
```

See [docs/foundry.md](docs/foundry.md) for model selection, preview, streaming,
receipts, security behavior, exit codes, and current backend gaps.

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
