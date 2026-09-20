#!/usr/bin/env python3
"""Velixar CLI — Memory API from your terminal."""

import os
import sys
import json
import click
import requests
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt

console = Console()

DEFAULT_BASE_URL = "https://api.velixarai.com"


def get_config():
    api_key = os.environ.get("VELIXAR_API_KEY", "")
    base_url = os.environ.get("VELIXAR_BASE_URL", DEFAULT_BASE_URL)
    if not api_key:
        config_path = os.path.expanduser("~/.velixar/config.json")
        if os.path.exists(config_path):
            with open(config_path) as f:
                cfg = json.load(f)
                api_key = cfg.get("api_key", "")
                base_url = cfg.get("base_url", base_url)
    return api_key, base_url


def api(method, path, **kwargs):
    api_key, base_url = get_config()
    if not api_key:
        console.print("[red]No API key configured. Run:[/red] velixar auth login")
        sys.exit(1)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = f"{base_url}{path}"
    resp = requests.request(method, url, headers=headers, **kwargs)
    if resp.status_code >= 400:
        console.print(f"[red]Error {resp.status_code}:[/red] {resp.text}")
        sys.exit(1)
    return resp.json()


@click.group()
@click.version_option(version="0.1.0", prog_name="velixar")
def cli():
    """Velixar — Memory API for AI applications."""
    pass


# ── Auth ──────────────────────────────────────────────

@cli.group()
def auth():
    """Manage authentication."""
    pass


@auth.command("login")
@click.option("--key", prompt="API Key", hide_input=True, help="Your Velixar API key (vlx_...)")
@click.option("--base-url", default=DEFAULT_BASE_URL, help="API base URL")
def auth_login(key, base_url):
    """Save your API key."""
    config_dir = os.path.expanduser("~/.velixar")
    os.makedirs(config_dir, exist_ok=True)
    with open(os.path.join(config_dir, "config.json"), "w") as f:
        json.dump({"api_key": key, "base_url": base_url}, f)
    os.chmod(os.path.join(config_dir, "config.json"), 0o600)
    console.print("[green]✓[/green] API key saved to ~/.velixar/config.json")


@auth.command("status")
def auth_status():
    """Check authentication status."""
    api_key, base_url = get_config()
    if not api_key:
        console.print("[red]✗[/red] Not authenticated. Run: velixar auth login")
        return
    masked = api_key[:7] + "..." + api_key[-4:] if len(api_key) > 11 else "***"
    console.print(f"[green]✓[/green] Authenticated — key: {masked}")
    console.print(f"  Base URL: {base_url}")
    try:
        resp = requests.get(f"{base_url}/health", timeout=10)
        data = resp.json()
        console.print(f"  API status: [green]{data.get('status', 'ok')}[/green]")
    except Exception:
        console.print("  API status: [red]unreachable[/red]")


# ── Store ─────────────────────────────────────────────

@cli.command()
@click.argument("content")
@click.option("--tier", "-t", type=int, default=None, help="Memory tier (0=pinned, 1=session, 2=semantic, 3=org)")
@click.option("--user-id", "-u", default=None, help="User ID namespace")
@click.option("--tags", default=None, help="Comma-separated tags")
@click.option("--metadata", "-m", default=None, help="JSON metadata")
def store(content, tier, user_id, tags, metadata):
    """Store a memory."""
    body = {"content": content}
    if tier is not None:
        body["tier"] = tier
    if user_id:
        body["user_id"] = user_id
    if tags:
        body["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if metadata:
        body["metadata"] = json.loads(metadata)
    data = api("POST", "/memory", json=body)
    mem_id = data.get("id", data.get("memory_id", "unknown"))
    console.print(f"[green]✓[/green] Stored: {mem_id}")


# ── Search ────────────────────────────────────────────

@cli.command()
@click.argument("query")
@click.option("--limit", "-n", default=5, help="Max results")
@click.option("--user-id", "-u", default=None, help="User ID namespace")
def search(query, limit, user_id):
    """Search memories by semantic query."""
    params = {"q": query, "limit": limit}
    if user_id:
        params["user_id"] = user_id
    data = api("GET", "/memory/search", params=params)
    memories = data if isinstance(data, list) else data.get("memories", data.get("results", []))
    if not memories:
        console.print("[dim]No memories found.[/dim]")
        return
    table = Table(title=f"Search: \"{query}\"", show_lines=True)
    table.add_column("ID", style="dim", max_width=16)
    table.add_column("Content", ratio=3)
    table.add_column("Tier", justify="center", width=6)
    table.add_column("Score", justify="right", width=8)
    for m in memories:
        content = m.get("content", "")[:120]
        table.add_row(
            str(m.get("id", ""))[:16],
            content,
            str(m.get("tier", "-")),
            f"{m.get('score', m.get('similarity', 0)):.3f}",
        )
    console.print(table)


# ── Get ───────────────────────────────────────────────

@cli.command()
@click.argument("memory_id")
def get(memory_id):
    """Get a specific memory by ID."""
    data = api("GET", f"/memory/{memory_id}")
    console.print(Panel(
        json.dumps(data, indent=2, default=str),
        title=f"Memory: {memory_id}",
        border_style="blue",
    ))


# ── List ──────────────────────────────────────────────

@cli.command("list")
@click.option("--limit", "-n", default=10, help="Max results")
@click.option("--user-id", "-u", default=None, help="User ID namespace")
def list_memories(limit, user_id):
    """List recent memories."""
    params = {"limit": limit}
    if user_id:
        params["user_id"] = user_id
    data = api("GET", "/memory/list", params=params)
    memories = data if isinstance(data, list) else data.get("memories", [])
    if not memories:
        console.print("[dim]No memories found.[/dim]")
        return
    table = Table(title="Memories", show_lines=True)
    table.add_column("ID", style="dim", max_width=16)
    table.add_column("Content", ratio=3)
    table.add_column("Tier", justify="center", width=6)
    for m in memories:
        table.add_row(str(m.get("id", ""))[:16], m.get("content", "")[:120], str(m.get("tier", "-")))
    console.print(table)


# ── Delete ────────────────────────────────────────────

@cli.command()
@click.argument("memory_id")
@click.confirmation_option(prompt="Are you sure?")
def delete(memory_id):
    """Delete a memory."""
    api("DELETE", f"/memory/{memory_id}")
    console.print(f"[green]✓[/green] Deleted: {memory_id}")


# ── Health ────────────────────────────────────────────

@cli.command()
def health():
    """Check API health."""
    _, base_url = get_config()
    try:
        resp = requests.get(f"{base_url}/health", timeout=10)
        data = resp.json()
    except Exception as e:
        console.print(f"[red]●[/red] API: unreachable ({e})")
        return
    status = data.get("status", "unknown")
    qdrant = data.get("qdrant", False)
    redis = data.get("redis", False)
    color = "green" if status == "healthy" else "red"
    console.print(f"[{color}]●[/{color}] API: {status}")
    console.print(f"  Qdrant: {'[green]✓[/green]' if qdrant else '[red]✗[/red]'}")
    console.print(f"  Redis:  {'[green]✓[/green]' if redis else '[red]✗[/red]'}")


# ── Interactive ───────────────────────────────────────

@cli.command()
@click.option("--user-id", "-u", default=None, help="User ID namespace")
def interactive(user_id):
    """Interactive memory shell."""
    console.print(Panel(
        "[bold]Velixar Interactive Shell[/bold]\n"
        "Commands: [cyan]store[/cyan] [cyan]search[/cyan] [cyan]list[/cyan] [cyan]get[/cyan] [cyan]delete[/cyan] [cyan]quit[/cyan]",
        border_style="purple",
    ))
    while True:
        try:
            cmd = Prompt.ask("\n[purple]velixar[/purple]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not cmd:
            continue
        parts = cmd.split(maxsplit=1)
        action = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if action in ("quit", "exit", "q"):
            break
        elif action == "store" and arg:
            body = {"content": arg}
            if user_id:
                body["user_id"] = user_id
            data = api("POST", "/memory", json=body)
            console.print(f"[green]✓[/green] Stored: {data.get('id', data.get('memory_id', ''))}")
        elif action == "search" and arg:
            params = {"q": arg, "limit": 5}
            if user_id:
                params["user_id"] = user_id
            data = api("GET", "/memory/search", params=params)
            memories = data if isinstance(data, list) else data.get("memories", data.get("results", []))
            for m in memories:
                score = m.get("score", m.get("similarity", 0))
                console.print(f"  [dim]{str(m.get('id',''))[:12]}[/dim] ({score:.3f}) {m.get('content','')[:100]}")
            if not memories:
                console.print("[dim]  No results.[/dim]")
        elif action == "list":
            params = {"limit": 10}
            if user_id:
                params["user_id"] = user_id
            data = api("GET", "/memory/list", params=params)
            memories = data if isinstance(data, list) else data.get("memories", [])
            for m in memories:
                console.print(f"  [dim]{str(m.get('id',''))[:12]}[/dim] [tier {m.get('tier','-')}] {m.get('content','')[:100]}")
        elif action == "get" and arg:
            data = api("GET", f"/memory/{arg.strip()}")
            console.print(json.dumps(data, indent=2, default=str))
        elif action == "delete" and arg:
            api("DELETE", f"/memory/{arg.strip()}")
            console.print("[green]✓[/green] Deleted")
        else:
            console.print("[dim]Commands: store <text> | search <query> | list | get <id> | delete <id> | quit[/dim]")

    console.print("[dim]Bye![/dim]")


# ── VOU beta meter ────────────────────────────────────
# Registered at the BOTTOM on purpose: velixar_vou calls back into `api()` above,
# so importing it any earlier would be a circular import. It lives in its own
# module because the command family is large and the HTTP plumbing above should
# stay the one place that knows how a Velixar request is authenticated.
from velixar_vou import vou as _vou_group  # noqa: E402

cli.add_command(_vou_group)


if __name__ == "__main__":
    cli()
