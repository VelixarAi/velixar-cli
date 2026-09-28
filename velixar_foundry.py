#!/usr/bin/env python3
"""`velixar foundry` — a safe client for the Velixar model gateway."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

import click


EXIT_CONFIG = 78
REDACTED = "[REDACTED]"

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,}\]]+)"),
    re.compile(
        r"(?i)([\"']?(?:api[_-]?key|x-api-key|ocp-apim-subscription-key)[\"']?\s*[:=]\s*[\"']?)"
        r"[^\s,}\]\"']+"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bvlx_[A-Za-z0-9_-]{4,}\b"),
    re.compile(r"\bsk-(?:live-|test-)?[A-Za-z0-9_-]{6,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@"),
)

_SENSITIVE_KEYS = frozenset({"authorization", "apikey", "xapikey", "ocpapimsubscriptionkey"})


class Redactor:
    """Redact known values and credential-shaped text before any Foundry output."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = tuple(sorted(
            {str(secret) for secret in secrets if secret and len(str(secret)) >= 8},
            key=len,
            reverse=True,
        ))

    @property
    def streaming_overlap(self) -> int:
        """Maximum tail held while streaming so a known credential cannot straddle writes."""
        return max((512, *(len(secret) for secret in self._secrets)))

    def text(self, value: Any) -> str:
        rendered = str(value)
        for secret in self._secrets:
            rendered = rendered.replace(secret, REDACTED)
        for index, pattern in enumerate(_SECRET_PATTERNS):
            if index in (0, 1):
                rendered = pattern.sub(lambda match: f"{match.group(1)}{REDACTED}", rendered)
            elif index == len(_SECRET_PATTERNS) - 1:
                rendered = pattern.sub(lambda match: f"{match.group(1)}{REDACTED}@", rendered)
            else:
                rendered = pattern.sub(REDACTED, rendered)
        return rendered

    def json_text(self, value: Any, *, pretty: bool = False) -> str:
        value = self.value(value)
        separators = None if pretty else (",", ":")
        return json.dumps(value, indent=2 if pretty else None, separators=separators,
                          ensure_ascii=False, default=str)

    def value(self, value: Any) -> Any:
        """Redact a JSON-compatible value without corrupting its structure or scalar types."""
        if isinstance(value, Mapping):
            redacted = {}
            for key, item in value.items():
                normalized = re.sub(r"[^a-z]", "", str(key).lower())
                redacted[key] = REDACTED if normalized in _SENSITIVE_KEYS else self.value(item)
            return redacted
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, tuple):
            return [self.value(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self.text(value)


@dataclass(frozen=True)
class FoundryConfig:
    gateway_url: Optional[str]
    api_key: str = field(repr=False)
    api_url: str


def _read_config(path: Path) -> Mapping[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def resolve_config(
    gateway_url: Optional[str] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    config_path: Optional[Path] = None,
) -> FoundryConfig:
    """Resolve flags > environment > user config > defaults without a gateway default."""
    env = os.environ if environ is None else environ
    path = config_path or Path(os.path.expanduser("~/.velixar/config.json"))
    saved = _read_config(path)
    gateway = _string(gateway_url) or _string(env.get("VELIXAR_GATEWAY_URL")) or _string(saved.get("gateway_url"))
    key = _string(env.get("VELIXAR_API_KEY")) or _string(saved.get("api_key")) or ""
    api_url = _string(env.get("VELIXAR_BASE_URL")) or _string(saved.get("base_url")) or "https://api.velixarai.com"
    return FoundryConfig(
        gateway_url=_safe_http_url(gateway) if gateway else None,
        api_key=key,
        api_url=_safe_http_url(api_url) or "https://api.velixarai.com",
    )


def _string(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _safe_http_url(value: str) -> Optional[str]:
    """Accept HTTP(S) endpoints without embedded credentials; normalize a trailing slash."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), parsed.query, ""))


@dataclass
class FoundryContext:
    config: FoundryConfig
    redactor: Redactor
    verbose: bool = False

    def write(self, value: Any = "", *, err: bool = False, nl: bool = True) -> None:
        click.echo(self.redactor.text(value), err=err, nl=nl)

    def write_json(self, value: Any, *, err: bool = False, pretty: bool = False) -> None:
        click.echo(self.redactor.json_text(value, pretty=pretty), err=err)

    def stop(self, message: str, code: int, *, json_mode: bool = False) -> None:
        if json_mode:
            self.write_json({"error": {"message": message}, "exit_code": code})
        else:
            self.write(message, err=True)
        raise click.exceptions.Exit(code)

    def require_gateway(self, *, json_mode: bool = False) -> str:
        if not self.config.gateway_url:
            self.stop("Foundry gateway not configured", EXIT_CONFIG, json_mode=json_mode)
        return self.config.gateway_url or ""

    def require_key(self, *, json_mode: bool = False) -> str:
        if not self.config.api_key:
            self.stop("No API key configured. Run: velixar auth login", EXIT_CONFIG, json_mode=json_mode)
        return self.config.api_key


@click.group()
@click.option("--gateway-url", metavar="URL", help="Velixar Foundry gateway URL.")
@click.option("--verbose", is_flag=True, help="Show safe server error metadata.")
@click.pass_context
def foundry(ctx: click.Context, gateway_url: Optional[str], verbose: bool) -> None:
    """Use Velixar Foundry models from the command line."""
    config = resolve_config(gateway_url)
    ctx.obj = FoundryContext(config=config, redactor=Redactor((config.api_key,)), verbose=verbose)


# Command modules register themselves on the group. Keep imports last to avoid cycles.
import velixar_foundry_catalog  # noqa: E402,F401
import velixar_foundry_run  # noqa: E402,F401
import velixar_foundry_resources  # noqa: E402,F401
