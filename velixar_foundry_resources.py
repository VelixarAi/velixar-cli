"""Receipts, agent-definition listing, and explicit backend-gap commands."""

from __future__ import annotations

from typing import Any, Dict, Optional

import click

from velixar_foundry import FoundryContext, foundry
from velixar_foundry_http import (
    EXIT_UNAVAILABLE, FoundryHTTPClient, HTTPFailure, Unreachable, emit_failure, response_json,
)


def _get(context: FoundryContext, path: str, *, json_mode: bool, params=None) -> Dict[str, Any]:
    context.require_gateway(json_mode=json_mode)
    context.require_key(json_mode=json_mode)
    client = FoundryHTTPClient(context)
    try:
        response = client.request("GET", context.config.api_url, path, params=params or {})
    except Unreachable:
        context.stop("Velixar API unreachable", EXIT_UNAVAILABLE, json_mode=json_mode)
    except HTTPFailure as failure:
        emit_failure(context, failure, json_mode=json_mode)
        raise AssertionError("unreachable")
    payload = response_json(response)
    if not payload:
        context.stop("Velixar returned an unreadable response", EXIT_UNAVAILABLE, json_mode=json_mode)
    return payload


@foundry.command()
@click.option("--before", help="ISO-8601 receipt cursor from a previous response.")
@click.option("--limit", type=click.IntRange(1, 100), default=50, show_default=True)
@click.option("--json", "json_mode", is_flag=True, help="Emit the server response as JSON.")
@click.pass_obj
def receipts(context: FoundryContext, before: Optional[str], limit: int, json_mode: bool) -> None:
    """List settled model-call receipts for the key's workspace."""
    params = {"limit": limit}
    if before:
        params["before"] = before
    payload = _get(context, "/v1/usage/receipts", json_mode=json_mode, params=params)
    if json_mode:
        context.write_json(payload)
        return
    rows = payload.get("receipts") if isinstance(payload.get("receipts"), list) else []
    context.write("Receipts:")
    if not rows:
        context.write("  none")
    for row in rows:
        context.write("  " + context.redactor.json_text(row))
    if payload.get("next_cursor"):
        context.write("Next cursor: " + context.redactor.json_text(payload["next_cursor"]))


@foundry.command()
@click.option("--json", "json_mode", is_flag=True, help="Emit the server response as JSON.")
@click.pass_obj
def agents(context: FoundryContext, json_mode: bool) -> None:
    """List agent definitions; these are not verified execution identities."""
    payload = _get(context, "/v1/agents/definitions", json_mode=json_mode)
    if json_mode:
        context.write_json({**payload, "label": "definitions - not verified identities"})
        return
    context.write("definitions - not verified identities")
    rows = payload.get("agents") if isinstance(payload.get("agents"), list) else []
    if not rows:
        context.write("  none")
    for row in rows:
        context.write("  " + context.redactor.json_text(row))


def _stub(name: str, gap: str):
    @foundry.command(name)
    @click.option("--json", "json_mode", is_flag=True, help="Emit the local unavailability result as JSON.")
    @click.pass_obj
    def command(context: FoundryContext, json_mode: bool) -> None:
        """Report a backend-dependent feature that is not available yet."""
        context.stop(f"not available yet ({gap})", EXIT_UNAVAILABLE, json_mode=json_mode)
    return command


_stub("tasks", "BG-1/2")
_stub("sessions", "BG-6")
_stub("context", "BG-7")
_stub("functions", "BG-8")
_stub("multi-agent", "BG-9")


@foundry.command()
@click.argument("execution_id")
@click.option("--json", "json_mode", is_flag=True, help="Emit the local unavailability result as JSON.")
@click.pass_obj
def inspect(context: FoundryContext, execution_id: str, json_mode: bool) -> None:
    """Inspect is unavailable until a durable execution resource exists."""
    context.stop("not available yet (BG-3)", EXIT_UNAVAILABLE, json_mode=json_mode)


@foundry.command()
@click.argument("execution_id")
@click.option("--json", "json_mode", is_flag=True, help="Emit the local unavailability result as JSON.")
@click.pass_obj
def resume(context: FoundryContext, execution_id: str, json_mode: bool) -> None:
    """Resume is unavailable while gateway requests remain stateless."""
    context.stop("not available yet (BG-3)", EXIT_UNAVAILABLE, json_mode=json_mode)


@foundry.command()
@click.option("--json", "json_mode", is_flag=True, help="Emit the local unavailability result as JSON.")
@click.pass_obj
def recommend(context: FoundryContext, json_mode: bool) -> None:
    """Recommendation requires server-owned routing and capability profiles."""
    context.stop("not available yet (BG-1/2)", EXIT_UNAVAILABLE, json_mode=json_mode)
