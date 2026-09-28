import json

import pytest
import requests
from click.testing import CliRunner

import velixar_foundry_run
from velixar_cli import cli
from velixar_foundry import Redactor
from velixar_foundry_run import ExecutionRequest, _redact_json_events


MODEL = {
    "id": "gpt-test",
    "object": "model",
    "velixar": {
        "display_name": "GPT Test",
        "recommended_for": ["edit"],
        "family": "openai",
        "surfaces": ["responses"],
        "capabilities": {"streaming": True, "tool_calling": True, "structured_output": False, "vision": False},
        "pricing": {"input_per_mtok_usd": 1.25, "output_per_mtok_usd": 10.0},
    },
}

CATALOG = {
    "object": "list",
    "data": [MODEL],
    "velixar": {
        "account": {"default_model": "gpt-test"},
        "not_included": [{"id": "gpt-locked", "reason_code": "model_not_eligible"}],
        "unavailable": [{**MODEL, "id": "gpt-down", "velixar": {**MODEL["velixar"], "status": "unavailable"}}],
    },
}


class Response:
    def __init__(self, status_code=200, payload=None, *, headers=None, lines=()):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self._lines = lines

    def json(self):
        return self._payload

    def iter_lines(self, decode_unicode=False):
        yield from self._lines

    def close(self):
        pass


def _sse(*events):
    lines = []
    for event in events:
        lines.extend([f"event: {event['type']}", "data: " + json.dumps(event), ""])
    return lines


def _invoke(args, monkeypatch, stream_events=None, stream_headers=None, input_text=None,
            api_key="vlx_testkey123"):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/v1/models"):
            return Response(payload=CATALOG)
        return Response(headers=stream_headers or {"X-Velixar-Response-Id": "resp_" + "a" * 32},
                        lines=_sse(*(stream_events or [])))

    monkeypatch.setattr(requests, "request", fake_request)
    result = CliRunner().invoke(
        cli, ["foundry", "--gateway-url", "https://gateway.example", "run", *args],
        env={"VELIXAR_API_KEY": api_key, "HOME": "/nonexistent"},
        input=input_text,
    )
    return result, calls


def test_execution_request_serializes_only_gateway_fields():
    request = ExecutionRequest(
        task_prompt="do work", model_strategy="exact", model_id="gpt-test", task_type="coding",
        workspace=None, context_mode="specified", metadata={"local": True}, max_output_tokens=200,
    )
    assert request.gateway_payload() == {
        "model": "gpt-test", "input": "do work", "stream": True, "store": False,
        "max_output_tokens": 200,
    }


def test_run_default_uses_only_server_advertised_default(monkeypatch):
    response_id = "resp_" + "a" * 32
    events = [
        {"type": "response.output_text.delta", "delta": "hello"},
        {"type": "response.completed", "response": {"id": response_id, "status": "completed"}},
        {"type": "velixar.charge", "response_id": response_id, "charge": "charged", "charged_usd": 0.01},
    ]
    result, calls = _invoke(["--prompt", "do work"], monkeypatch, events)
    assert result.exit_code == 0, result.output
    post = calls[1]
    assert post[1] == "https://gateway.example/v1/responses"
    assert post[2]["json"]["model"] == "gpt-test"
    assert "Auto: Velixar default for your plan" in result.output
    assert "hello" in result.output
    assert not {"user_id", "principal_id", "workspace_id", "provider", "resolved_model"}.intersection(post[2]["json"])


def test_exact_unavailable_never_posts_or_falls_back(monkeypatch):
    result, calls = _invoke(["--prompt", "do work", "--model", "gpt-down"], monkeypatch)
    assert result.exit_code == 69
    assert "Requested model unavailable." in result.output
    assert "Server reason: unavailable" in result.output
    assert len(calls) == 1


def test_exact_callable_model_stays_exact(monkeypatch):
    response_id = "resp_" + "a" * 32
    events = [
        {"type": "response.completed", "response": {"id": response_id, "status": "completed"}},
        {"type": "velixar.charge", "response_id": response_id, "charge": "charged"},
    ]
    result, calls = _invoke(["--prompt", "x", "--model", "gpt-test"], monkeypatch, events)
    assert result.exit_code == 0
    assert calls[1][2]["json"]["model"] == "gpt-test"
    assert "Exact model: gpt-test" in result.output


def test_exact_not_included_and_unknown_are_distinct_nonzero(monkeypatch):
    locked, calls = _invoke(["--prompt", "x", "--model", "gpt-locked"], monkeypatch)
    assert locked.exit_code == 77 and len(calls) == 1
    assert "Server reason: model_not_eligible" in locked.output
    missing, calls = _invoke(["--prompt", "x", "--model", "gpt-missing"], monkeypatch)
    assert missing.exit_code == 79 and len(calls) == 1


def test_capability_list_is_informational_only(monkeypatch):
    result, calls = _invoke(["--capability", "tool_calling", "--list", "--json"], monkeypatch)
    assert result.exit_code == 0
    assert [item["id"] for item in json.loads(result.output)["data"]] == ["gpt-test"]
    assert len(calls) == 1 and calls[0][0] == "GET"


def test_unsupported_authority_flags_stop_before_network(monkeypatch):
    for args, marker in ((["--workspace", "forged"], "BG-4"),
                         (["--agent-name", "CISO"], "BG-5"),
                         (["--context", "full"], "BG-7"),
                         (["--function", "shell"], "BG-8"),
                         (["--task", "function"], "BG-8"),
                         (["--multi-agent"], "BG-9"),
                         (["--capability", "streaming"], "BG-2")):
        result, calls = _invoke(["--prompt", "x", *args], monkeypatch)
        assert result.exit_code == 69
        assert marker in result.output
        assert calls == []


def test_json_stream_is_ndjson_and_redacts_split_delta_secret(monkeypatch):
    secret = "vlx_splitsecret123"
    response_id = "resp_" + "a" * 32
    events = [
        {"type": "response.output_text.delta", "delta": "token vlx_split"},
        {"type": "response.output_text.delta", "delta": "secret123 done"},
        {"type": "response.completed", "response": {"id": response_id, "status": "completed"}},
        {"type": "velixar.charge", "response_id": response_id, "charge": "charged"},
    ]
    result, _ = _invoke(["--prompt", "x", "--json"], monkeypatch, events)
    assert result.exit_code == 0, result.output
    decoded = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(decoded) == 4
    assert secret not in result.stdout + result.stderr
    deltas = "".join(event.get("delta", "") for event in decoded)
    assert "[REDACTED]" in deltas


def test_text_stream_redacts_secret_split_across_emission_boundary(monkeypatch):
    response_id = "resp_" + "a" * 32
    padding = "a" * 1599 + " "
    events = [
        {"type": "response.output_text.delta", "delta": padding + "vlx_split"},
        {"type": "response.output_text.delta", "delta": "secret123 done"},
        {"type": "response.completed", "response": {"id": response_id, "status": "completed"}},
        {"type": "velixar.charge", "response_id": response_id, "charge": "charged"},
    ]
    result, _ = _invoke(["--prompt", "x"], monkeypatch, events)
    assert result.exit_code == 0
    assert "vlx_splitsecret123" not in result.output
    assert "[REDACTED]" in result.output


@pytest.mark.parametrize(("secret", "configured"), [
    ("configured-secret-without-shape", True),
    ("vlx_other_stream_secret_123456", False),
    ("sk-live-providerstreamsecret123456", False),
    ("AIzaSyA1234567890abcdefghijklmnopqrstuvw", False),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ2ZWxpeGFyIn0.signature_part", False),
])
@pytest.mark.parametrize("json_mode", [False, True])
def test_stream_redacts_each_secret_class_across_deltas(monkeypatch, secret, configured, json_mode):
    response_id = "resp_" + "a" * 32
    split = len(secret) // 2
    events = [
        {"type": "response.output_text.delta", "delta": "before " + secret[:split]},
        {"type": "response.output_text.delta", "delta": secret[split:] + " after"},
        {"type": "response.completed", "response": {"id": response_id, "status": "completed"}},
        {"type": "velixar.charge", "response_id": response_id, "charge": "charged"},
    ]
    args = ["--prompt", "x", *(["--json"] if json_mode else [])]
    result, _ = _invoke(
        args, monkeypatch, events,
        api_key=secret if configured else "configured-key-without-a-secret-shape",
    )
    assert result.exit_code == 0, result.output
    assert secret not in result.stdout + result.stderr
    if json_mode:
        rendered = "".join(json.loads(line).get("delta", "") for line in result.stdout.splitlines())
        assert "[REDACTED]" in rendered
    else:
        assert "[REDACTED]" in result.output


def test_quiet_suppresses_model_text_but_keeps_provenance(monkeypatch):
    response_id = "resp_" + "a" * 32
    events = [
        {"type": "response.output_text.delta", "delta": "hidden"},
        {"type": "response.completed", "response": {"id": response_id, "status": "completed"}},
        {"type": "velixar.charge", "response_id": response_id, "charge": "charged"},
    ]
    result, _ = _invoke(["--prompt", "x", "--quiet"], monkeypatch, events)
    assert result.exit_code == 0
    assert "hidden" not in result.output
    assert "Provenance:" in result.output


def test_preview_is_deterministic_and_never_posts(monkeypatch):
    first, calls = _invoke(["--prompt", "same", "--preview", "--json"], monkeypatch)
    second, _ = _invoke(["--prompt", "same", "--preview", "--json"], monkeypatch)
    assert first.output == second.output
    payload = json.loads(first.output)
    assert payload["preview"]["cost_estimate"] == "not available yet (BG-11)"
    assert payload["preview"]["list_rates"] == MODEL["velixar"]["pricing"]
    assert len(calls) == 1


def test_server_error_message_is_verbatim_and_exit_is_mapped(monkeypatch):
    message = "This request could not start. Nothing was charged."
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/v1/models"):
            return Response(payload=CATALOG)
        return Response(402, {"error": {"message": message, "reason_code": "allowance_exhausted"}})

    monkeypatch.setattr(requests, "request", fake_request)
    result = CliRunner().invoke(
        cli, ["foundry", "--gateway-url", "https://gateway.example", "run", "--prompt", "x"],
        env={"VELIXAR_API_KEY": "vlx_testkey123", "HOME": "/nonexistent"},
    )
    assert result.exit_code == 5
    assert result.output.endswith(message + "\n")


def test_ctrl_c_cancels_only_header_response_id(monkeypatch):
    response_id = "resp_" + "b" * 32
    calls = []

    class Interrupted(Response):
        def iter_lines(self, decode_unicode=False):
            raise KeyboardInterrupt
            yield  # pragma: no cover

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/v1/models"):
            return Response(payload=CATALOG)
        if url.endswith("/v1/responses"):
            return Interrupted(headers={"X-Velixar-Response-Id": response_id})
        return Response(payload={"id": response_id, "status": "cancelled"})

    monkeypatch.setattr(requests, "request", fake_request)
    result = CliRunner().invoke(
        cli, ["foundry", "--gateway-url", "https://gateway.example", "run", "--prompt", "x"],
        env={"VELIXAR_API_KEY": "vlx_testkey123", "HOME": "/nonexistent"},
    )
    assert result.exit_code == 130
    assert calls[-1][0:2] == ("POST", f"https://gateway.example/v1/responses/{response_id}/cancel")


def test_ctrl_c_does_not_guess_after_response_id_mismatch(monkeypatch):
    header_id = "resp_" + "b" * 32
    event_id = "resp_" + "c" * 32
    calls = []

    class Mismatched(Response):
        def iter_lines(self, decode_unicode=False):
            yield "event: response.created"
            yield "data: " + json.dumps({"type": "response.created", "response": {"id": event_id}})
            yield ""
            raise KeyboardInterrupt

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/v1/models"):
            return Response(payload=CATALOG)
        return Mismatched(headers={"X-Velixar-Response-Id": header_id})

    monkeypatch.setattr(requests, "request", fake_request)
    result = CliRunner().invoke(
        cli, ["foundry", "--gateway-url", "https://gateway.example", "run", "--prompt", "x"],
        env={"VELIXAR_API_KEY": "vlx_testkey123", "HOME": "/nonexistent"},
    )
    assert result.exit_code == 130
    assert "Cancellation unavailable" in result.output
    assert len(calls) == 2


def test_terminal_server_error_message_and_exit(monkeypatch):
    response_id = "resp_" + "a" * 32
    message = "GPT Test is temporarily unavailable. Nothing was charged for this request."
    events = [{
        "type": "response.failed",
        "response": {"id": response_id, "status": "failed",
                     "error": {"message": message, "reason_code": "model_unavailable"}},
    }]
    result, _ = _invoke(["--prompt", "x"], monkeypatch, events)
    assert result.exit_code == 69
    assert message in result.output


def test_failed_terminal_without_error_is_not_success(monkeypatch):
    response_id = "resp_" + "a" * 32
    events = [{"type": "response.failed", "response": {"id": response_id, "status": "failed"}}]
    result, _ = _invoke(["--prompt", "x"], monkeypatch, events)
    assert result.exit_code == 1
    assert "Foundry response failed" in result.output


def test_interactive_prompt_uses_same_request_builder(monkeypatch):
    response_id = "resp_" + "a" * 32
    events = [
        {"type": "response.completed", "response": {"id": response_id, "status": "completed"}},
        {"type": "velixar.charge", "response_id": response_id, "charge": "charged"},
    ]
    monkeypatch.setattr(velixar_foundry_run, "_stdin_is_tty", lambda: True)
    result, calls = _invoke([], monkeypatch, events, input_text="interactive\n")
    assert result.exit_code == 0, result.output
    assert calls[1][2]["json"] == ExecutionRequest(
        task_prompt="interactive", model_strategy="auto", model_id="gpt-test"
    ).gateway_payload()


def test_split_event_redaction_preserves_event_count_and_reconstructed_text():
    events = [
        {"type": "response.output_text.delta", "delta": "before vlx_split"},
        {"type": "response.output_text.delta", "delta": "secret123 after"},
    ]
    result = _redact_json_events(events, Redactor())
    assert len(result) == 2
    assert "".join(event["delta"] for event in result) == "before [REDACTED] after"


def test_sse_parser_accepts_crlf():
    from velixar_foundry_run import iter_sse

    parsed = list(iter_sse(["event: response.output_text.delta\r", 'data: {"type":"x"}\r', "\r"]))
    assert parsed == [("response.output_text.delta", '{"type":"x"}')]


def test_missing_default_never_posts(monkeypatch):
    broken = {**CATALOG, "velixar": {**CATALOG["velixar"], "account": {"default_model": "absent"}}}
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response(payload=broken)

    monkeypatch.setattr(requests, "request", fake_request)
    result = CliRunner().invoke(
        cli, ["foundry", "--gateway-url", "https://gateway.example", "run", "--prompt", "x"],
        env={"VELIXAR_API_KEY": "vlx_testkey123", "HOME": "/nonexistent"},
    )
    assert result.exit_code == 69
    assert len(calls) == 1


def test_auto_flag_uses_default_and_preview_payload_has_no_authority_fields(monkeypatch):
    result, calls = _invoke(["--prompt", "x", "--model", "auto", "--preview", "--json"], monkeypatch)
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["request"]["model"] == "gpt-test"
    assert not {"user_id", "principal_id", "workspace_id", "provider", "resolved_model"}.intersection(
        payload["request"]
    )
    assert len(calls) == 1


def test_auto_flag_posts_catalog_default_never_literal_auto(monkeypatch):
    response_id = "resp_" + "a" * 32
    events = [
        {"type": "response.completed", "response": {"id": response_id, "status": "completed"}},
        {"type": "velixar.charge", "response_id": response_id, "charge": "charged"},
    ]
    result, calls = _invoke(["--prompt", "x", "--model", "auto"], monkeypatch, events)
    assert result.exit_code == 0, result.output
    assert calls[1][2]["json"]["model"] == "gpt-test"
    assert "auto" not in calls[1][2]["json"].values()
