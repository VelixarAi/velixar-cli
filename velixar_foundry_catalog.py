"""Status and live-model discovery commands for `velixar foundry`."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

import click

from velixar_foundry import FoundryContext, foundry
from velixar_foundry_http import (
    EXIT_UNAVAILABLE, FoundryHTTPClient, HTTPFailure, Unreachable, emit_failure, response_json,
)


def _probe(client: FoundryHTTPClient, method: str, base: str, path: str, *, authenticated: bool) -> Dict[str, Any]:
    try:
        response = client.request(method, base, path, authenticated=authenticated)
        return {"state": "ok", "status_code": response.status_code, "body": response_json(response)}
    except Unreachable:
        return {"state": "unreachable"}
    except HTTPFailure as failure:
        state = "not enabled" if failure.response.status_code == 404 else (
            "key refused" if failure.response.status_code in (401, 403) else "unreachable"
        )
        return {"state": state, "status_code": failure.response.status_code, "body": failure.payload}


@foundry.command()
@click.option("--json", "json_mode", is_flag=True, help="Emit one JSON object.")
@click.pass_obj
def status(context: FoundryContext, json_mode: bool) -> None:
    """Check gateway health, readiness, account access, and build usage."""
    gateway = context.require_gateway(json_mode=json_mode)
    context.require_key(json_mode=json_mode)
    client = FoundryHTTPClient(context)
    health = _probe(client, "GET", gateway, "/health", authenticated=False)
    ready = _probe(client, "GET", gateway, "/health/ready", authenticated=False)
    models = _probe(client, "GET", gateway, "/v1/models", authenticated=True)
    usage = _probe(client, "GET", context.config.api_url, "/v1/usage", authenticated=True)

    if health["state"] == "unreachable" or ready["state"] == "unreachable":
        overall = "unreachable"
    elif models["state"] == "not enabled":
        overall = "not enabled"
    elif models["state"] == "key refused" or usage["state"] == "key refused":
        overall = "key refused"
    elif all(item["state"] == "ok" for item in (health, ready, models, usage)):
        overall = "ok"
    else:
        overall = "unreachable"

    payload = {
        "state": overall,
        "gateway": {
            "health": health["state"],
            "ready": ready["state"],
            "git_sha": health.get("body", {}).get("git_sha"),
            "checks": ready.get("body", {}).get("checks"),
        },
        "account": models.get("body", {}).get("velixar", {}).get("account"),
        "build": usage.get("body", {}).get("build"),
    }
    if json_mode:
        context.write_json(payload)
        return
    context.write(f"Foundry: {overall}")
    context.write(f"  health: {health['state']}")
    context.write(f"  ready: {ready['state']}")
    if payload["account"] is not None:
        context.write("  account: " + context.redactor.json_text(payload["account"]))
    if payload["build"] is not None:
        context.write("  build: " + context.redactor.json_text(payload["build"]))


def _field(entry: Dict[str, Any], name: str) -> Any:
    extension = entry.get("velixar")
    return extension.get(name) if isinstance(extension, dict) else None


def _matches(entry: Dict[str, Any], capability: Optional[str], family: Optional[str],
             surface: Optional[str], recommended_for: Optional[str]) -> bool:
    caps = _field(entry, "capabilities") or {}
    return (
        (capability is None or bool(caps.get(capability)))
        and (family is None or _field(entry, "family") == family)
        and (surface is None or surface in (_field(entry, "surfaces") or []))
        and (recommended_for is None or recommended_for in (_field(entry, "recommended_for") or []))
    )


def filter_models(entries: Iterable[Dict[str, Any]], capability: Optional[str] = None,
                  family: Optional[str] = None, surface: Optional[str] = None,
                  recommended_for: Optional[str] = None):
    return [entry for entry in entries if isinstance(entry, dict) and
            _matches(entry, capability, family, surface, recommended_for)]


@foundry.command()
@click.option("--capability", type=click.Choice(["tool_calling", "structured_output", "streaming"]))
@click.option("--family")
@click.option("--surface")
@click.option("--recommended-for")
@click.option("--json", "json_mode", is_flag=True, help="Emit the filtered server catalog as JSON.")
@click.pass_obj
def models(context: FoundryContext, capability: Optional[str], family: Optional[str], surface: Optional[str],
           recommended_for: Optional[str], json_mode: bool) -> None:
    """List models from the live Velixar gateway catalog."""
    gateway = context.require_gateway(json_mode=json_mode)
    context.require_key(json_mode=json_mode)
    client = FoundryHTTPClient(context)
    try:
        response = client.request("GET", gateway, "/v1/models")
    except Unreachable:
        context.stop("Foundry gateway unreachable", EXIT_UNAVAILABLE, json_mode=json_mode)
    except HTTPFailure as failure:
        if failure.response.status_code == 404 and failure.error.get("reason_code") is None:
            emit_failure(context, failure, json_mode=json_mode, exit_code=EXIT_UNAVAILABLE)
        emit_failure(context, failure, json_mode=json_mode)
        return

    payload = response_json(response)
    if not payload:
        context.stop("Foundry returned an unreadable model catalog", EXIT_UNAVAILABLE,
                     json_mode=json_mode)
    extension = payload.get("velixar") if isinstance(payload.get("velixar"), dict) else {}
    rendered = {
        "object": payload.get("object", "list"),
        "data": filter_models(payload.get("data") or [], capability, family, surface, recommended_for),
        "velixar": {
            **{key: value for key, value in extension.items() if key not in ("not_included", "unavailable")},
            "not_included": filter_models(extension.get("not_included") or [], capability, family,
                                           surface, recommended_for),
            "unavailable": filter_models(extension.get("unavailable") or [], capability, family,
                                          surface, recommended_for),
        },
    }
    if json_mode:
        context.write_json(rendered)
        return
    account = rendered["velixar"].get("account") or {}
    context.write(f"Default model: {account.get('default_model') or 'none'}")
    for heading, entries in (("Available", rendered["data"]),
                             ("Not included", rendered["velixar"]["not_included"]),
                             ("Unavailable", rendered["velixar"]["unavailable"])):
        context.write(f"{heading}:")
        if not entries:
            context.write("  none")
        for entry in entries:
            context.write("  " + context.redactor.json_text(entry))
