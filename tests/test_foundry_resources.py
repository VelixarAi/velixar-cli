import json

import requests
from click.testing import CliRunner

from velixar_cli import cli


class Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _invoke(args, monkeypatch, payload):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response(payload=payload)

    monkeypatch.setattr(requests, "request", fake_request)
    result = CliRunner().invoke(
        cli, ["foundry", "--gateway-url", "https://gateway.example", *args],
        env={"VELIXAR_API_KEY": "vlx_testkey123", "VELIXAR_BASE_URL": "https://memory.example", "HOME": "/nonexistent"},
    )
    return result, calls


def test_receipts_uses_verified_endpoint_and_cursor(monkeypatch):
    payload = {"receipts": [{"request_id": "gw_1", "model_id": "gpt-test", "charged_usd": 0.2}],
               "next_cursor": "2026-09-01T00:00:00Z"}
    result, calls = _invoke(["receipts", "--before", "2026-09-02T00:00:00Z", "--limit", "25", "--json"],
                            monkeypatch, payload)
    assert result.exit_code == 0
    assert json.loads(result.output) == payload
    assert calls == [("GET", "https://memory.example/v1/usage/receipts", {
        "headers": {"Authorization": "Bearer vlx_testkey123", "Accept": "application/json"},
        "timeout": 10.0,
        "params": {"limit": 25, "before": "2026-09-02T00:00:00Z"},
    })]


def test_agents_are_labelled_definitions_not_identities(monkeypatch):
    result, calls = _invoke(["agents", "--json"], monkeypatch,
                            {"label": "verified identity", "agents": [{"id": "agent-1", "name": "CISO"}]})
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["label"] == "definitions - not verified identities"
    assert len(calls) == 1
    assert calls[0][0:2] == ("GET", "https://memory.example/v1/agents/definitions")


def test_agents_human_label_is_first(monkeypatch):
    result, _ = _invoke(["agents"], monkeypatch, {"agents": [{"id": "agent-1", "name": "CISO"}]})
    assert result.exit_code == 0
    assert result.output.splitlines()[0] == "definitions - not verified identities"


def test_backend_gap_commands_exit_69_without_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network called")

    monkeypatch.setattr(requests, "request", forbidden)
    cases = [
        (["tasks"], "BG-1/2"),
        (["sessions"], "BG-6"),
        (["context"], "BG-7"),
        (["functions"], "BG-8"),
        (["multi-agent"], "BG-9"),
        (["inspect", "exec_1"], "BG-3"),
        (["resume", "exec_1"], "BG-3"),
        (["recommend"], "BG-1/2"),
    ]
    for args, marker in cases:
        result = CliRunner().invoke(cli, ["foundry", *args], env={"HOME": "/nonexistent"})
        assert result.exit_code == 69
        assert result.output == f"not available yet ({marker})\n"


def test_backend_gap_commands_support_json(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network called")

    monkeypatch.setattr(requests, "request", forbidden)
    result = CliRunner().invoke(cli, ["foundry", "functions", "--json"], env={"HOME": "/nonexistent"})
    assert result.exit_code == 69
    assert json.loads(result.output) == {"error": {"message": "not available yet (BG-8)"}, "exit_code": 69}


def test_all_resource_commands_have_help():
    runner = CliRunner()
    for command in ("receipts", "agents", "tasks", "sessions", "context", "functions",
                    "multi-agent", "inspect", "resume", "recommend"):
        result = runner.invoke(cli, ["foundry", command, "--help"])
        assert result.exit_code == 0, (command, result.output)
