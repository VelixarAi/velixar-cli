import json

import click
import pytest
from click.testing import CliRunner

from velixar_cli import cli
from velixar_foundry import FoundryConfig, FoundryContext, REDACTED, Redactor, resolve_config


def test_foundry_namespace_has_help():
    result = CliRunner().invoke(cli, ["foundry", "--help"])
    assert result.exit_code == 0
    assert "Use Velixar Foundry models" in result.output
    assert "--gateway-url URL" in result.output


def test_gateway_url_precedence(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"gateway_url": "https://config.example/", "api_key": "config-key"}))
    env = {"VELIXAR_GATEWAY_URL": "https://env.example/", "VELIXAR_API_KEY": "env-key"}

    assert resolve_config("https://flag.example/", environ=env, config_path=path).gateway_url == "https://flag.example"
    assert resolve_config(environ=env, config_path=path).gateway_url == "https://env.example"
    assert resolve_config(environ={}, config_path=path).gateway_url == "https://config.example"


def test_key_uses_existing_env_then_config_mechanism(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api_key": "config-key"}))
    assert resolve_config(environ={"VELIXAR_API_KEY": "env-key"}, config_path=path).api_key == "env-key"
    assert resolve_config(environ={}, config_path=path).api_key == "config-key"


def test_no_default_gateway_url(tmp_path):
    config = resolve_config(environ={}, config_path=tmp_path / "missing.json")
    assert config.gateway_url is None


def test_missing_gateway_uses_required_copy_and_exit(capsys):
    context = FoundryContext(FoundryConfig(None, "", "https://api.example"), Redactor())
    with pytest.raises(click.exceptions.Exit) as caught:
        context.require_gateway()
    assert caught.value.exit_code == 78
    assert capsys.readouterr().err == "Foundry gateway not configured\n"


def test_redactor_removes_known_and_shaped_secrets():
    known = "provider-secret-without-a-shape"
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature_part"
    value = (
        f"Authorization: Bearer vlx_supersecret123 api-key=sk-live-abcdef123456 "
        f"token={jwt} raw={known}"
    )
    result = Redactor((known,)).text(value)
    assert "vlx_supersecret123" not in result
    assert "sk-live-abcdef123456" not in result
    assert jwt not in result
    assert known not in result
    assert REDACTED in result


def test_redactor_removes_unlabelled_velixar_key():
    secret = "vlx_planted_secret_123456"
    assert Redactor().text("leak " + secret) == "leak " + REDACTED


def test_redactor_keeps_json_valid():
    raw = {"authorization": "Bearer vlx_secret123", "ok": "visible", "enabled": True, "count": 1234}
    rendered = Redactor(("true", "1234")).json_text(raw)
    decoded = json.loads(rendered)
    assert decoded["authorization"] == REDACTED
    assert decoded["ok"] == "visible"
    assert decoded["enabled"] is True
    assert decoded["count"] == 1234


def test_redactor_removes_url_userinfo_and_avoids_hostname_false_positive():
    value = "https://user:password@example.com path abcdefgh.ijklmnop.qrstuvwx"
    result = Redactor().text(value)
    assert "user:password" not in result
    assert "abcdefgh.ijklmnop.qrstuvwx" in result


def test_invalid_config_types_and_credentialed_gateway_fail_closed(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"gateway_url": ["https://bad.example"], "api_key": 1234}))
    config = resolve_config(environ={}, config_path=path)
    assert config.gateway_url is None
    assert config.api_key == ""

    config = resolve_config("https://user:password@gateway.example", environ={}, config_path=path)
    assert config.gateway_url is None


def test_context_output_methods_apply_redaction(capsys):
    secret = "vlx_supersecret123"
    context = FoundryContext(FoundryConfig("https://gateway.example", secret, "https://api.example"),
                             Redactor((secret,)))
    context.write(f"normal {secret}")
    context.write(f"error {secret}", err=True)
    context.write_json({"api_key": secret, "message": f"nested {secret}"})
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert captured.out.count(REDACTED) == 3
    assert captured.err.count(REDACTED) == 1


def test_config_repr_and_unknown_json_values_do_not_leak_key():
    secret = "vlx_supersecret123"
    config = FoundryConfig("https://gateway.example", secret, "https://api.example")
    redactor = Redactor((secret,))
    assert secret not in repr(config)
    rendered = redactor.json_text({"config": config})
    assert secret not in rendered
    assert REDACTED not in rendered  # repr omits the field rather than exposing it for replacement
