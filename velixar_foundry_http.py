"""HTTP and server-error handling shared by Foundry CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import click
import requests

from velixar_foundry import EXIT_CONFIG, FoundryContext


EXIT_FAILURE = 1
EXIT_ALLOWANCE = 5
EXIT_DATA = 65
EXIT_UNAVAILABLE = 69
EXIT_TEMPORARY = 75
EXIT_DAILY = 76
EXIT_PERMISSION = 77
EXIT_NOT_FOUND = 79

_DATA_REASONS = {
    "model_surface_mismatch", "input_too_long", "max_output_tokens_too_large",
    "unsupported_parameter", "invalid_request", "model_rejected_request", "duplicate_request",
}
_UNAVAILABLE_REASONS = {
    "key_store_unavailable", "budget_unresolved", "entitlement_unresolved",
    "rate_limiter_unavailable", "model_unavailable", "model_timeout",
}
_TEMPORARY_REASONS = {"rate_limited", "concurrency_limit_reached"}
_DAILY_REASONS = {"free_capacity_exhausted_today", "daily_request_limit_reached"}
_PERMISSION_REASONS = {
    "auth_invalid", "scope_missing_models_invoke", "scope_missing_models_list", "email_not_verified",
    "build_preview_not_enabled", "build_not_included_in_plan", "subscription_not_active",
    "model_not_eligible", "credits_not_eligible",
}
_ALLOWANCE_REASONS = {
    "allowance_exhausted", "request_exceeds_remaining", "request_exceeds_per_request_cap",
    "credits_exhausted", "vou_exhausted",
}


def exit_code_for(status: int, reason: Optional[str]) -> int:
    if reason in _ALLOWANCE_REASONS or status == 402:
        return EXIT_ALLOWANCE
    if reason in _DATA_REASONS:
        return EXIT_DATA
    if reason in _UNAVAILABLE_REASONS:
        return EXIT_UNAVAILABLE
    if reason in _TEMPORARY_REASONS:
        return EXIT_TEMPORARY
    if reason in _DAILY_REASONS:
        return EXIT_DAILY
    if reason in _PERMISSION_REASONS:
        return EXIT_PERMISSION
    if reason == "auth_missing":
        return EXIT_CONFIG
    if reason in {"model_not_found", "response_not_found"}:
        return EXIT_NOT_FOUND
    if status == 400 or status == 409:
        return EXIT_DATA
    if status in (401, 403):
        return EXIT_PERMISSION
    if status == 404:
        return EXIT_NOT_FOUND
    if status == 429:
        return EXIT_TEMPORARY
    if status in (503, 504):
        return EXIT_UNAVAILABLE
    return EXIT_FAILURE


@dataclass
class HTTPFailure(Exception):
    response: requests.Response

    @property
    def payload(self) -> Dict[str, Any]:
        try:
            value = self.response.json()
        except (ValueError, requests.RequestException):
            return {"error": {"message": "Foundry returned an unreadable error response"}}
        return value if isinstance(value, dict) else {"error": {"message": "Foundry returned an unreadable error response"}}

    @property
    def error(self) -> Dict[str, Any]:
        value = self.payload.get("error")
        return value if isinstance(value, dict) else {"message": "Foundry request failed"}

    @property
    def exit_code(self) -> int:
        return exit_code_for(self.response.status_code, self.error.get("reason_code"))


class Unreachable(Exception):
    pass


def response_json(response: requests.Response) -> Dict[str, Any]:
    try:
        value = response.json()
    except (ValueError, requests.RequestException):
        return {}
    return value if isinstance(value, dict) else {}


class FoundryHTTPClient:
    def __init__(self, context: FoundryContext, *, timeout: float = 10.0) -> None:
        self.context = context
        self.timeout = timeout

    def request(self, method: str, base_url: str, path: str, *, authenticated: bool = True, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        if authenticated:
            if not self.context.config.api_key:
                self.context.stop("No API key configured. Run: velixar auth login", EXIT_CONFIG)
            headers["Authorization"] = f"Bearer {self.context.config.api_key}"
        headers.setdefault("Accept", "application/json")
        try:
            response = requests.request(
                method, f"{base_url.rstrip('/')}{path}", headers=headers,
                timeout=kwargs.pop("timeout", self.timeout), **kwargs,
            )
        except requests.RequestException as exc:
            raise Unreachable(type(exc).__name__) from None
        if response.status_code >= 400:
            raise HTTPFailure(response)
        return response


def emit_failure(context: FoundryContext, failure: HTTPFailure, *, json_mode: bool = False,
                 exit_code: Optional[int] = None) -> None:
    payload = failure.payload
    code = failure.exit_code if exit_code is None else exit_code
    if json_mode:
        output = dict(payload)
        output.setdefault("exit_code", code)
        context.write_json(output)
    else:
        context.write(failure.error.get("message") or "Foundry request failed", err=True)
        if context.verbose:
            metadata = {key: failure.error.get(key) for key in
                        ("code", "reason_code", "request_id", "retry_after") if failure.error.get(key) is not None}
            if metadata:
                context.write(context.redactor.json_text(metadata), err=True)
    raise click.exceptions.Exit(code)
