import json
from pathlib import Path

import pytest
import requests
from click.testing import CliRunner

from velixar_cli import cli
from velixar_foundry import REDACTED, Redactor


SECRETS = (
    "vlx_planted_secret_123456",
    "sk-live-provider-secret-123456",
    "AIzaSyA1234567890abcdefghijklmnopqrstuvw",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ2ZWxpeGFyIn0.signature_part",
)


def assert_no_secrets(value):
    for secret in SECRETS:
        assert secret not in value


def test_redaction_corpus_in_normal_error_debug_and_json_output(monkeypatch):
    key, provider, google, jwt = SECRETS
    model = {
        "id": "gpt-test", "object": "model",
        "velixar": {"surfaces": ["responses"], "capabilities": {"streaming": True},
                    "pricing": {"note": provider}},
    }
    catalog = {"object": "list", "data": [model],
               "velixar": {"account": {"default_model": "gpt-test", "debug": jwt},
                            "not_included": [], "unavailable": []}}

    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return catalog

    monkeypatch.setattr(requests, "request", lambda *args, **kwargs: Response())
    runner = CliRunner()
    env = {"VELIXAR_API_KEY": key, "HOME": "/nonexistent"}

    normal = runner.invoke(cli, ["foundry", "--gateway-url", "https://gateway.example", "models"], env=env)
    machine = runner.invoke(cli, ["foundry", "--gateway-url", "https://gateway.example", "models", "--json"], env=env)
    preview = runner.invoke(cli, ["foundry", "--gateway-url", "https://gateway.example", "run",
                                  "--prompt", f"{key} {google}", "--preview"], env=env)
    for result in (normal, machine, preview):
        assert result.exit_code == 0, result.output
        assert_no_secrets(result.output)
        assert REDACTED in result.output


def test_nonverbose_server_error_keeps_copy_but_redacts_secret(monkeypatch):
    key = SECRETS[0]

    class Response:
        status_code = 403
        headers = {}

        def json(self):
            return {"error": {"message": f"Access refused for {key}.",
                              "reason_code": "model_not_eligible"}}

    monkeypatch.setattr(requests, "request", lambda *args, **kwargs: Response())
    result = CliRunner().invoke(
        cli, ["foundry", "--gateway-url", "https://gateway.example", "models"],
        env={"VELIXAR_API_KEY": key, "HOME": "/nonexistent"},
    )
    assert result.exit_code == 77
    assert result.output == f"Access refused for {REDACTED}.\n"


def test_verbose_server_error_redacts_all_metadata(monkeypatch):
    key, provider, google, jwt = SECRETS

    class Response:
        status_code = 403
        headers = {}

        def json(self):
            return {"error": {"message": f"refused {key}", "code": "DENIED",
                              "reason_code": "model_not_eligible", "request_id": jwt,
                              "retry_after": provider}}

    monkeypatch.setattr(requests, "request", lambda *args, **kwargs: Response())
    result = CliRunner().invoke(
        cli, ["foundry", "--gateway-url", "https://gateway.example", "--verbose", "models"],
        env={"VELIXAR_API_KEY": key, "HOME": "/nonexistent"},
    )
    assert result.exit_code == 77
    assert_no_secrets(result.output)
    assert result.output.count(REDACTED) >= 3
    assert "refused" in result.output
    assert "DENIED" in result.output


def test_transport_exception_never_prints_exception_or_key(monkeypatch):
    key = SECRETS[0]

    def fail(*args, **kwargs):
        raise requests.ConnectionError(f"Authorization: Bearer {key}")

    monkeypatch.setattr(requests, "request", fail)
    result = CliRunner().invoke(
        cli, ["foundry", "--gateway-url", "https://gateway.example", "models"],
        env={"VELIXAR_API_KEY": key, "HOME": "/nonexistent"},
    )
    assert result.exit_code == 69
    assert_no_secrets(result.output)
    lowered = result.output.lower()
    for forbidden in ("traceback", "connectionerror", "requests.exceptions", "authorization", "bearer"):
        assert forbidden not in lowered
    assert result.exception is not None
    assert "Authorization" not in str(result.exception)


def test_authorization_and_provider_header_values_are_redacted():
    key, provider, _, _ = SECRETS
    value = {
        "Authorization": f"Bearer {key}",
        "api-key": provider,
        "x-api-key": provider,
        "Ocp-Apim-Subscription-Key": provider,
        "safe": "visible",
    }
    rendered = Redactor((key,)).json_text(value)
    assert_no_secrets(rendered)
    decoded = json.loads(rendered)
    assert all(decoded[name] == REDACTED for name in value if name != "safe")


def test_json_error_fields_cross_cli_redaction_boundary(monkeypatch):
    key, provider, _, _ = SECRETS

    class Response:
        status_code = 403
        headers = {}

        def json(self):
            return {
                "error": {
                    "message": "refused safely",
                    "reason_code": "model_not_eligible",
                    "Authorization": f"Bearer {key}",
                    "x-api-key": provider,
                },
                "api-key": provider,
            }

    monkeypatch.setattr(requests, "request", lambda *args, **kwargs: Response())
    result = CliRunner().invoke(
        cli, ["foundry", "--gateway-url", "https://gateway.example", "models", "--json"],
        env={"VELIXAR_API_KEY": key, "HOME": "/nonexistent"},
    )
    assert result.exit_code == 77
    assert_no_secrets(result.output)
    payload = json.loads(result.output)
    assert payload["error"]["Authorization"] == REDACTED
    assert payload["error"]["x-api-key"] == REDACTED
    assert payload["api-key"] == REDACTED


def test_unconfigured_velixar_key_is_redacted_through_cli(monkeypatch):
    configured_key = "configured-secret-without-shape"
    other_key = "vlx_other_key_not_configured"

    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {
                "object": "list",
                "data": [{"id": "gpt-test", "velixar": {"note": other_key}}],
                "velixar": {"account": {"default_model": "gpt-test"},
                            "not_included": [], "unavailable": []},
            }

    monkeypatch.setattr(requests, "request", lambda *args, **kwargs: Response())
    result = CliRunner().invoke(
        cli, ["foundry", "--gateway-url", "https://gateway.example", "models", "--json"],
        env={"VELIXAR_API_KEY": configured_key, "HOME": "/nonexistent"},
    )
    assert result.exit_code == 0
    assert other_key not in result.output
    assert REDACTED in result.output


def test_config_file_api_key_joins_redaction_set(monkeypatch, tmp_path: Path):
    config_key = "config-provider-secret-without-shape"
    monkeypatch.delenv("VELIXAR_API_KEY", raising=False)
    monkeypatch.delenv("VELIXAR_GATEWAY_URL", raising=False)
    config_dir = tmp_path / ".velixar"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({
        "gateway_url": "https://gateway.example",
        "api_key": config_key,
    }))
    sent_headers = {}

    class Response:
        status_code = 403
        headers = {}

        def json(self):
            return {"error": {"message": f"Refused {config_key}",
                              "reason_code": "model_not_eligible"}}

    def request(*args, **kwargs):
        sent_headers.update(kwargs["headers"])
        return Response()

    monkeypatch.setattr(requests, "request", request)
    result = CliRunner().invoke(cli, ["foundry", "models"], env={"HOME": str(tmp_path)})
    assert result.exit_code == 77
    assert sent_headers["Authorization"] == f"Bearer {config_key}"
    assert config_key not in result.output
    assert result.output == f"Refused {REDACTED}\n"


@pytest.mark.parametrize("secret", SECRETS)
def test_planted_secret_positive_control_goes_red(secret):
    with pytest.raises(AssertionError):
        assert_no_secrets("unsafe output: " + secret)
