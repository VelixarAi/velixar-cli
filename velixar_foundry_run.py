"""One-request execution, SSE rendering, and scoped cancellation for Foundry."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional, Tuple

import click

from velixar_foundry import FoundryContext, Redactor, foundry
from velixar_foundry_catalog import filter_models
from velixar_foundry_http import (
    EXIT_DATA,
    EXIT_NOT_FOUND,
    EXIT_PERMISSION,
    EXIT_UNAVAILABLE,
    FoundryHTTPClient,
    HTTPFailure,
    Unreachable,
    emit_failure,
    exit_code_for,
    response_json,
)


TASK_TYPES = (
    "coding", "architecture", "planning", "research", "security_review", "data_analysis",
    "extraction", "documentation", "agent_workflow", "function", "custom",
)
CAPABILITIES = ("tool_calling", "structured_output", "streaming")
PROHIBITED_GATEWAY_FIELDS = frozenset({"user_id", "principal_id", "workspace_id", "provider", "resolved_model"})


@dataclass(frozen=True)
class ExecutionRequest:
    task_prompt: str
    model_strategy: str
    model_id: str
    task_type: str = "custom"
    workspace: Optional[str] = None
    session: Optional[str] = None
    agent: Optional[str] = None
    capability_profile: Optional[str] = None
    context_mode: str = "specified"
    requested_functions: Tuple[str, ...] = ()
    execution_mode: str = "single"
    budget: Optional[Mapping[str, Any]] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    max_output_tokens: Optional[int] = None

    def gateway_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model_id,
            "input": self.task_prompt,
            "stream": True,
            "store": False,
        }
        if self.max_output_tokens is not None:
            payload["max_output_tokens"] = self.max_output_tokens
        assert not PROHIBITED_GATEWAY_FIELDS.intersection(payload)
        return payload


class StreamRedactor:
    """Delay a bounded tail so credentials split across transport chunks are redacted."""

    HOLD = 1024

    def __init__(self, redactor: Redactor) -> None:
        self.redactor = redactor
        self.pending = ""

    def feed(self, text: str) -> str:
        self.pending += text
        if len(self.pending) <= self.HOLD:
            return ""
        cut = max(0, len(self.pending) - self.HOLD - self.redactor.streaming_overlap)
        scan_start = max(0, cut - self.HOLD)
        for marker in ("Authorization", "Bearer", "vlx_", "sk-", "AIza", "eyJ"):
            position = self.pending.rfind(marker, scan_start)
            if position != -1:
                cut = min(cut, position)
        safe, self.pending = self.pending[:cut], self.pending[cut:]
        return self.redactor.text(safe)

    def finish(self) -> str:
        result = self.redactor.text(self.pending)
        self.pending = ""
        return result


def iter_sse(lines: Iterable[Any]) -> Iterator[Tuple[Optional[str], str]]:
    event = None
    data = []
    for raw in lines:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        if line.endswith("\r"):
            line = line[:-1]
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = None, []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[6:] if line[5:6] == " " else line[5:])
    if data:
        yield event, "\n".join(data)


def _catalog(context: FoundryContext, client: FoundryHTTPClient, gateway: str, json_mode: bool) -> Dict[str, Any]:
    try:
        response = client.request("GET", gateway, "/v1/models")
    except Unreachable:
        context.stop("Foundry gateway unreachable", EXIT_UNAVAILABLE, json_mode=json_mode)
    except HTTPFailure as failure:
        emit_failure(context, failure, json_mode=json_mode)
        raise AssertionError("unreachable")
    payload = response_json(response)
    if not payload:
        context.stop("Foundry returned an unreadable model catalog", EXIT_UNAVAILABLE,
                     json_mode=json_mode)
    return payload


def _find(entries: Iterable[Dict[str, Any]], model_id: str) -> Optional[Dict[str, Any]]:
    return next((entry for entry in entries if isinstance(entry, dict) and entry.get("id") == model_id), None)


def _select_model(context: FoundryContext, catalog: Dict[str, Any], requested: Optional[str],
                  json_mode: bool) -> Tuple[str, str, Dict[str, Any]]:
    data = catalog.get("data") or []
    extension = catalog.get("velixar") if isinstance(catalog.get("velixar"), dict) else {}
    account = extension.get("account") if isinstance(extension.get("account"), dict) else {}
    if requested in (None, "auto"):
        model_id = account.get("default_model")
        entry = _find(data, model_id) if isinstance(model_id, str) else None
        if entry is None:
            context.stop("Requested model unavailable.", EXIT_UNAVAILABLE, json_mode=json_mode)
        return "auto", model_id, entry

    entry = _find(data, requested)
    if entry is not None:
        return "exact", requested, entry
    locked = _find(extension.get("not_included") or [], requested)
    unavailable = _find(extension.get("unavailable") or [], requested)
    detail = locked or unavailable
    reason = None
    if detail:
        reason = detail.get("reason_code") or (
            detail.get("velixar", {}).get("status") if isinstance(detail.get("velixar"), dict) else None
        )
    message = "Requested model unavailable." + (f"\nServer reason: {reason}" if reason else "")
    code = EXIT_PERMISSION if locked else EXIT_UNAVAILABLE if unavailable else EXIT_NOT_FOUND
    context.stop(message, code, json_mode=json_mode)
    raise AssertionError("unreachable")


def _preview(request: ExecutionRequest, entry: Dict[str, Any]) -> Dict[str, Any]:
    extension = entry.get("velixar") if isinstance(entry.get("velixar"), dict) else {}
    return {
        "task_type": request.task_type,
        "model_strategy": request.model_strategy,
        "model_id": request.model_id,
        "model_label": (f"Auto: Velixar default for your plan -> {request.model_id}"
                        if request.model_strategy == "auto" else f"Exact model: {request.model_id}"),
        "surface": "responses",
        "stream": True,
        "store": False,
        "context": "specified prompt only; memory context not available (BG-7)",
        "workspace": "cannot verify workspace (BG-4)",
        "requested_functions": [],
        "list_rates": extension.get("pricing") or "not supplied",
        "cost_estimate": "not available yet (BG-11)",
        "prompt": request.task_prompt,
    }


def _print_preview(context: FoundryContext, preview: Dict[str, Any], *, err: bool) -> None:
    context.write("Execution preview", err=err)
    for key in ("task_type", "model_label", "surface", "stream", "store", "context", "workspace",
                "requested_functions", "list_rates", "cost_estimate"):
        context.write(f"{key.replace('_', ' ').title()}: {context.redactor.json_text(preview[key])}", err=err)
    context.write("Prompt:", err=err)
    context.write(preview["prompt"], err=err)


def _terminal_error(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    response = event.get("response")
    if not isinstance(response, dict):
        return None
    error = response.get("error")
    return error if isinstance(error, dict) else None


def _redact_json_events(events: list[Dict[str, Any]], redactor: Redactor) -> list[Dict[str, Any]]:
    """Preserve event order/shape while catching secrets split across text-delta events."""
    delta_events = [event for event in events if event.get("type") == "response.output_text.delta"
                    and isinstance(event.get("delta"), str)]
    if delta_events:
        combined = redactor.text("".join(event["delta"] for event in delta_events))
        offset = 0
        for index, event in enumerate(delta_events):
            if index == len(delta_events) - 1:
                event["delta"] = combined[offset:]
            else:
                width = min(len(event["delta"]), max(0, len(combined) - offset))
                event["delta"] = combined[offset:offset + width]
                offset += width
    return events


def _event_response_id(event: Dict[str, Any]) -> Optional[str]:
    direct = event.get("response_id")
    if isinstance(direct, str):
        return direct
    response = event.get("response")
    return response.get("id") if isinstance(response, dict) and isinstance(response.get("id"), str) else None


def _cancel(client: FoundryHTTPClient, gateway: str, response_id: Optional[str]) -> None:
    if not response_id:
        return
    try:
        client.request("POST", gateway, f"/v1/responses/{response_id}/cancel")
    except (HTTPFailure, Unreachable):
        pass


def _stream(context: FoundryContext, client: FoundryHTTPClient, gateway: str, request: ExecutionRequest,
            *, quiet: bool, json_mode: bool) -> None:
    try:
        response = client.request("POST", gateway, "/v1/responses", json=request.gateway_payload(),
                                  stream=True, timeout=900)
    except Unreachable:
        context.stop("Foundry gateway unreachable", EXIT_UNAVAILABLE, json_mode=json_mode)
    except HTTPFailure as failure:
        emit_failure(context, failure, json_mode=json_mode)
        return

    response_id = response.headers.get("X-Velixar-Response-Id")
    response_id_conflict = False
    terminal_error = None
    terminal_type = None
    json_events = []
    text_redactor = StreamRedactor(context.redactor)
    saw_terminal = False
    saw_charge = False
    interrupted = False
    cancel_requested = False
    try:
        for event_name, raw_data in iter_sse(response.iter_lines(decode_unicode=True)):
            if raw_data == "[DONE]":
                continue
            try:
                event = json.loads(raw_data)
            except ValueError:
                context.stop("Foundry returned an unreadable stream event", EXIT_UNAVAILABLE,
                             json_mode=json_mode)
            if not isinstance(event, dict):
                context.stop("Foundry returned an unreadable stream event", EXIT_UNAVAILABLE,
                             json_mode=json_mode)
            event_id = _event_response_id(event)
            if event_id and response_id and event_id != response_id:
                response_id = None
                response_id_conflict = True
            elif event_id and response_id is None and not response_id_conflict:
                response_id = event_id
            event_type = event.get("type") or event_name
            if event_type in ("response.completed", "response.incomplete", "response.failed"):
                saw_terminal = True
                terminal_type = event_type
                terminal_error = _terminal_error(event)
                if event_type != "response.completed" and terminal_error is None:
                    terminal_error = {"message": "Foundry response failed", "reason_code": "internal_error"}
            if event_type == "velixar.charge":
                saw_charge = True
            if json_mode:
                json_events.append(event)
            elif event_type == "response.output_text.delta" and not quiet:
                context.write(text_redactor.feed(str(event.get("delta") or "")), nl=False)
            elif event_type == "velixar.charge":
                context.write("\nProvenance: " + context.redactor.json_text(event), err=True)
    except KeyboardInterrupt:
        _cancel(client, gateway, response_id)
        cancel_requested = bool(response_id)
        interrupted = True
    finally:
        if not json_mode and not quiet:
            context.write(text_redactor.finish(), nl=False)
        if not saw_terminal and response_id and not cancel_requested:
            _cancel(client, gateway, response_id)
        try:
            response.close()
        except Exception:
            pass

    if interrupted:
        context.write("Cancellation requested." if cancel_requested else
                      "Cancellation unavailable: no in-flight response id was received.", err=True)
        raise click.exceptions.Exit(130)

    if json_mode:
        for event in _redact_json_events(json_events, context.redactor):
            context.write_json(event)
    elif not quiet:
        context.write()
    if response_id:
        context.write(f"Response: {response_id}", err=True)
    if terminal_error or terminal_type in ("response.failed", "response.incomplete"):
        terminal_error = terminal_error or {"message": "Foundry response failed", "reason_code": "internal_error"}
        context.write(terminal_error.get("message") or "Foundry response failed", err=True)
        raise click.exceptions.Exit(exit_code_for(0, terminal_error.get("reason_code")))
    if not saw_terminal and not saw_charge:
        context.stop("Stream interrupted; execution outcome is unknown.", EXIT_UNAVAILABLE,
                     json_mode=json_mode)


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


@foundry.command()
@click.option("--prompt", help="Task prompt. Prompts interactively when omitted in a TTY.")
@click.option("--task", "task_type", type=click.Choice(TASK_TYPES), default="custom", show_default=True)
@click.option("--model", help="Exact live model id, or 'auto' for the account default.")
@click.option("--capability", type=click.Choice(CAPABILITIES))
@click.option("--list", "list_only", is_flag=True, help="List models; capability is informational only.")
@click.option("--workspace", help="Consistency check (unavailable until BG-4).")
@click.option("--session", help="Session id (unavailable until BG-6).")
@click.option("--agent-name", help="Agent display name (unavailable until BG-5).")
@click.option("--context", "context_mode", type=click.Choice(["specified", "session", "workspace", "full"]))
@click.option("--function", "functions", multiple=True, help="Requested function (unavailable until BG-8).")
@click.option("--multi-agent", is_flag=True, help="Multi-agent execution (unavailable until BG-9).")
@click.option("--max-output-tokens", type=click.IntRange(min=1))
@click.option("--preview", is_flag=True, help="Validate and show the request without executing it.")
@click.option("--quiet", is_flag=True, help="Suppress generated text; still show provenance.")
@click.option("--json", "json_mode", is_flag=True, help="Emit redacted server events as NDJSON.")
@click.pass_obj
def run(context: FoundryContext, prompt: Optional[str], task_type: str, model: Optional[str],
        capability: Optional[str], list_only: bool, workspace: Optional[str], session: Optional[str],
        agent_name: Optional[str], context_mode: Optional[str], functions: Tuple[str, ...], multi_agent: bool,
        max_output_tokens: Optional[int], preview: bool, quiet: bool, json_mode: bool) -> None:
    """Preview or run one stateless Responses request."""
    if workspace:
        context.stop("cannot verify workspace (BG-4)", EXIT_UNAVAILABLE, json_mode=json_mode)
    if session:
        context.stop("not available yet (BG-6)", EXIT_UNAVAILABLE, json_mode=json_mode)
    if agent_name:
        context.stop("not available yet (BG-5)", EXIT_UNAVAILABLE, json_mode=json_mode)
    if context_mode:
        context.stop("not available yet (BG-7)", EXIT_UNAVAILABLE, json_mode=json_mode)
    if functions:
        context.stop("not available yet (BG-8)", EXIT_UNAVAILABLE, json_mode=json_mode)
    if multi_agent:
        context.stop("not available yet (BG-9)", EXIT_UNAVAILABLE, json_mode=json_mode)
    if task_type == "function":
        context.stop("not available yet (BG-8)", EXIT_UNAVAILABLE, json_mode=json_mode)
    if quiet and json_mode:
        context.stop("--quiet cannot be combined with --json", 2, json_mode=True)
    if capability and not list_only:
        context.stop("not available yet (BG-2)", EXIT_UNAVAILABLE, json_mode=json_mode)

    gateway = context.require_gateway(json_mode=json_mode)
    context.require_key(json_mode=json_mode)
    client = FoundryHTTPClient(context)
    catalog = _catalog(context, client, gateway, json_mode)
    if list_only:
        entries = filter_models(catalog.get("data") or [], capability=capability)
        if json_mode:
            context.write_json({"object": "list", "data": entries})
        else:
            for entry in entries:
                context.write(context.redactor.json_text(entry))
        return

    if prompt is None:
        if not _stdin_is_tty():
            context.stop("Prompt required in non-interactive mode; use --prompt", 2, json_mode=json_mode)
        prompt = click.prompt("What are you trying to accomplish?", type=str)
    if not prompt.strip():
        context.stop("Prompt must not be empty", 2, json_mode=json_mode)

    strategy, model_id, entry = _select_model(context, catalog, model, json_mode)
    surfaces = entry.get("velixar", {}).get("surfaces") if isinstance(entry.get("velixar"), dict) else []
    if "responses" not in (surfaces or []):
        context.stop("Requested model unavailable.\nServer reason: model_surface_mismatch",
                     EXIT_DATA, json_mode=json_mode)
    execution = ExecutionRequest(
        task_prompt=prompt,
        task_type=task_type,
        model_strategy=strategy,
        model_id=model_id,
        max_output_tokens=max_output_tokens,
    )
    rendered_preview = _preview(execution, entry)
    if preview:
        if json_mode:
            context.write_json({"preview": rendered_preview, "request": execution.gateway_payload()})
        else:
            _print_preview(context, rendered_preview, err=False)
        return
    if not json_mode:
        _print_preview(context, rendered_preview, err=True)
    _stream(context, client, gateway, execution, quiet=quiet, json_mode=json_mode)
