import json

import pytest
import requests
from click.testing import CliRunner

from velixar_cli import cli
from velixar_foundry_http import exit_code_for


class Response:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


MODEL = {
    "id": "gpt-test",
    "object": "model",
    "owned_by": "velixar",
    "velixar": {
        "display_name": "GPT Test",
        "recommended_for": ["review"],
        "family": "openai",
        "surfaces": ["responses"],
        "capabilities": {"streaming": True, "tool_calling": False, "structured_output": True, "vision": False},
        "status": "available",
        "pricing": {"input_per_mtok_usd": 1.0, "output_per_mtok_usd": 2.0},
    },
}

MODELS = {
    "object": "list",
    "data": [MODEL],
    "velixar": {
        "account": {"tier": "cortex", "default_model": "gpt-test"},
        "not_included": [{"id": "gpt-locked", "display_name": "Locked", "reason_code": "model_not_eligible"}],
        "unavailable": [{**MODEL, "id": "gpt-down", "velixar": {**MODEL["velixar"], "status": "unavailable"}}],
    },
}


def _invoke(args, monkeypatch, handler):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        result = handler(method, url, kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(requests, "request", fake_request)
    result = CliRunner().invoke(
        cli,
        ["foundry", "--gateway-url", "https://gateway.example", *args],
        env={"VELIXAR_API_KEY": "vlx_testkey123", "VELIXAR_BASE_URL": "https://memory.example", "HOME": "/nonexistent"},
    )
    return result, calls


def test_status_calls_all_contract_endpoints_and_whitelists_output(monkeypatch):
    def handler(method, url, kwargs):
        if url.endswith("/health"):
            return Response(payload={"status": "ok", "git_sha": "abc"})
        if url.endswith("/health/ready"):
            return Response(payload={"status": "ready", "checks": {"db": True}})
        if url.endswith("/v1/models"):
            return Response(payload=MODELS)
        return Response(payload={"build": {"state": "resolved", "remaining_usd": 3}, "history": "private"})

    result, calls = _invoke(["status", "--json"], monkeypatch, handler)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["state"] == "ok"
    assert payload["account"]["default_model"] == "gpt-test"
    assert payload["build"]["remaining_usd"] == 3
    assert "history" not in result.output
    assert [call[1] for call in calls] == [
        "https://gateway.example/health",
        "https://gateway.example/health/ready",
        "https://gateway.example/v1/models",
        "https://memory.example/v1/usage",
    ]
    assert "Authorization" not in calls[0][2]["headers"]
    assert "Authorization" not in calls[1][2]["headers"]
    assert calls[2][2]["headers"]["Authorization"].startswith("Bearer vlx_")
    assert calls[3][2]["headers"]["Authorization"].startswith("Bearer vlx_")


def test_status_distinguishes_unreachable_not_enabled_and_key_refused(monkeypatch):
    scenarios = {
        "unreachable": lambda url: requests.ConnectionError("secret should not print"),
        "not enabled": lambda url: Response(404, {"error": {"message": "Not found"}}) if url.endswith("/v1/models") else Response(),
        "key refused": lambda url: Response(401, {"error": {"message": "Invalid key", "reason_code": "auth_invalid"}})
        if url.endswith("/v1/models") else Response(),
    }
    for expected, factory in scenarios.items():
        result, _ = _invoke(["status", "--json"], monkeypatch,
                            lambda method, url, kwargs, factory=factory: factory(url))
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["state"] == expected


def test_models_json_preserves_real_entries_and_filters(monkeypatch):
    result, calls = _invoke(
        ["models", "--capability", "structured_output", "--family", "openai", "--surface", "responses",
         "--recommended-for", "review", "--json"],
        monkeypatch,
        lambda method, url, kwargs: Response(payload=MODELS),
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"] == [MODEL]
    assert payload["velixar"]["unavailable"][0]["id"] == "gpt-down"
    assert payload["velixar"]["not_included"] == []
    assert calls[0][1].endswith("/v1/models")


def test_models_unfiltered_preserves_all_three_real_catalog_lists(monkeypatch):
    result, _ = _invoke(["models", "--json"], monkeypatch,
                        lambda method, url, kwargs: Response(payload=MODELS))
    payload = json.loads(result.output)
    assert [item["id"] for item in payload["data"]] == ["gpt-test"]
    assert [item["id"] for item in payload["velixar"]["not_included"]] == ["gpt-locked"]
    assert [item["id"] for item in payload["velixar"]["unavailable"]] == ["gpt-down"]


def test_models_prints_server_error_verbatim_and_maps_exit(monkeypatch):
    message = "This API key does not have the models:list scope. Nothing was charged."
    result, _ = _invoke(
        ["models"], monkeypatch,
        lambda method, url, kwargs: Response(403, {"error": {"message": message, "reason_code": "scope_missing_models_list"}}),
    )
    assert result.exit_code == 77
    assert result.output == message + "\n"


def test_models_404_disabled_is_unavailable(monkeypatch):
    result, _ = _invoke(
        ["models"], monkeypatch,
        lambda method, url, kwargs: Response(404, {"error": {"message": "Not found"}}),
    )
    assert result.exit_code == 69
    assert result.output == "Not found\n"


def test_models_never_uses_network_when_gateway_missing(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("network called")

    monkeypatch.setattr(requests, "request", forbidden)
    result = CliRunner().invoke(cli, ["foundry", "models"],
                                env={"HOME": str(tmp_path), "VELIXAR_API_KEY": "vlx_testkey123"})
    assert result.exit_code == 78
    assert result.output == "Foundry gateway not configured\n"


def test_models_json_errors_are_single_json_values(monkeypatch, tmp_path):
    missing = CliRunner().invoke(cli, ["foundry", "models", "--json"],
                                 env={"HOME": str(tmp_path), "VELIXAR_API_KEY": "vlx_testkey123"})
    assert missing.exit_code == 78
    assert json.loads(missing.output)["exit_code"] == 78

    unreachable, _ = _invoke(["models", "--json"], monkeypatch,
                             lambda method, url, kwargs: requests.ConnectionError("vlx_must_not_leak"))
    assert unreachable.exit_code == 69
    assert json.loads(unreachable.output)["exit_code"] == 69
    assert "vlx_must_not_leak" not in unreachable.output


def test_models_json_error_preserves_server_envelope(monkeypatch):
    body = {"error": {"message": "Not found", "code": "NOT_FOUND"}, "request_id": "req_123"}
    result, _ = _invoke(["models", "--json"], monkeypatch,
                        lambda method, url, kwargs: Response(404, body))
    assert result.exit_code == 69
    payload = json.loads(result.output)
    assert payload["request_id"] == "req_123"
    assert payload["error"] == body["error"]
    assert payload["exit_code"] == 69


def test_models_invalid_success_payload_fails(monkeypatch):
    class InvalidResponse(Response):
        def json(self):
            raise ValueError("not json")

    result, _ = _invoke(["models", "--json"], monkeypatch,
                        lambda method, url, kwargs: InvalidResponse())
    assert result.exit_code == 69
    assert json.loads(result.output)["error"]["message"] == "Foundry returned an unreadable model catalog"


@pytest.mark.parametrize(("status", "reason", "expected"), [
    (402, "allowance_exhausted", 5),
    (400, "invalid_request", 65),
    (503, "model_unavailable", 69),
    (429, "rate_limited", 75),
    (429, "daily_request_limit_reached", 76),
    (403, "model_not_eligible", 77),
    (401, "auth_missing", 78),
    (404, "model_not_found", 79),
    (500, "unknown_future_reason", 1),
    (429, None, 75),
])
def test_exit_code_catalog(status, reason, expected):
    assert exit_code_for(status, reason) == expected
