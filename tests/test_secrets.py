"""Secrets come from a gitignored file; the environment always wins."""

import asyncio
import os

import httpx
import pytest

from evaling.config import Case, Message, ModelSpec
from evaling.engine import run_eval
from evaling.providers import create_provider
from evaling.providers.base import CompletionRequest, ProviderError
from evaling.providers.openai import OpenAIProvider
from evaling.render import render_messages
from evaling.secrets import (
    PROJECT_SECRETS_NAME,
    SecretsError,
    build_env,
    load_secrets,
    redact,
    world_readable,
)
from helpers import make_config, make_settings


def write_secrets(path, body, mode=0o600):
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


posix_only = pytest.mark.skipif(
    os.name != "posix", reason="POSIX mode bits; Windows ACLs are checked differently"
)


class TestLoading:
    def test_project_file_supplies_keys(self, tmp_path):
        write_secrets(tmp_path / PROJECT_SECRETS_NAME, "ANTHROPIC_API_KEY: sk-from-file\n")
        env, warnings = build_env(tmp_path, env={})
        assert env["ANTHROPIC_API_KEY"] == "sk-from-file"
        assert warnings == []

    def test_real_environment_wins(self, tmp_path):
        # CI sets a real env var; the file must not override it.
        write_secrets(tmp_path / PROJECT_SECRETS_NAME, "ANTHROPIC_API_KEY: sk-from-file\n")
        env, _ = build_env(tmp_path, env={"ANTHROPIC_API_KEY": "sk-from-env"})
        assert env["ANTHROPIC_API_KEY"] == "sk-from-env"

    def test_explicit_path_via_env_var(self, tmp_path):
        custom = write_secrets(tmp_path / "elsewhere.yaml", "OPENAI_API_KEY: sk-custom\n")
        env, _ = build_env(tmp_path, env={"EVALING_SECRETS": str(custom)})
        assert env["OPENAI_API_KEY"] == "sk-custom"

    def test_explicit_missing_path_is_an_error(self, tmp_path):
        # Silently ignoring a path the user named would be baffling.
        with pytest.raises(SecretsError, match="points at a missing file"):
            build_env(tmp_path, env={"EVALING_SECRETS": str(tmp_path / "nope.yaml")})

    def test_no_files_is_fine(self, tmp_path):
        env, warnings = build_env(tmp_path, env={})
        assert warnings == []
        assert "ANYTHING" not in env

    def test_empty_file_is_fine(self, tmp_path):
        write_secrets(tmp_path / PROJECT_SECRETS_NAME, "")
        assert build_env(tmp_path, env={})[1] == []

    def test_values_are_stringified(self, tmp_path):
        write_secrets(tmp_path / PROJECT_SECRETS_NAME, "SOME_PORT: 8080\n")
        env, _ = build_env(tmp_path, env={})
        assert env["SOME_PORT"] == "8080"

    def test_non_mapping_rejected(self, tmp_path):
        write_secrets(tmp_path / PROJECT_SECRETS_NAME, "- just\n- a list\n")
        with pytest.raises(SecretsError, match="expected a mapping"):
            load_secrets(tmp_path, env={})

    def test_nested_value_rejected(self, tmp_path):
        write_secrets(tmp_path / PROJECT_SECRETS_NAME, "KEY:\n  nested: value\n")
        with pytest.raises(SecretsError, match="must be a scalar"):
            load_secrets(tmp_path, env={})

    def test_invalid_yaml_rejected(self, tmp_path):
        write_secrets(tmp_path / PROJECT_SECRETS_NAME, "KEY: [unclosed\n")
        with pytest.raises(SecretsError, match="invalid YAML"):
            load_secrets(tmp_path, env={})

    @posix_only
    def test_loose_permissions_warn(self, tmp_path):
        path = write_secrets(tmp_path / PROJECT_SECRETS_NAME, "K: v\n", mode=0o644)
        assert world_readable(path)
        _, warnings = build_env(tmp_path, env={})
        assert any("readable by other users" in w for w in warnings)

    def test_tight_permissions_do_not_warn(self, tmp_path):
        write_secrets(tmp_path / PROJECT_SECRETS_NAME, "K: v\n", mode=0o600)
        assert build_env(tmp_path, env={})[1] == []


class TestProviderIntegration:
    def test_provider_authenticates_from_the_file(self, tmp_path):
        write_secrets(tmp_path / PROJECT_SECRETS_NAME, "OPENAI_API_KEY: sk-secret-file\n")
        env, _ = build_env(tmp_path, env={})
        spec = ModelSpec.model_validate({"id": "gpt-5.2", "provider": "openai"})
        provider = create_provider(spec, env)

        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
            )

        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        messages = render_messages([Message(role="user", content="hi")], Case(), tmp_path)

        async def go():
            try:
                return await provider.complete(CompletionRequest(model=spec, messages=messages))
            finally:
                await provider.aclose()

        assert asyncio.run(go()).text == "ok"
        assert seen[0].headers["authorization"] == "Bearer sk-secret-file"

    def test_missing_key_still_errors_clearly(self, tmp_path):
        env, _ = build_env(tmp_path, env={})
        spec = ModelSpec.model_validate({"id": "gpt-5.2", "provider": "openai"})
        provider = create_provider(spec, env)
        with pytest.raises(ProviderError, match="no API key found"):
            provider.api_key()

    def test_secret_values_are_redacted_from_errors(self, tmp_path):
        # Even a secret belonging to a *different* model must not be echoed.
        write_secrets(
            tmp_path / PROJECT_SECRETS_NAME,
            "OPENAI_API_KEY: sk-mine-12345678\nOTHER_SERVICE_TOKEN: tok-abcdefgh\n",
        )
        env, _ = build_env(tmp_path, env={})
        spec = ModelSpec.model_validate({"id": "gpt-5.2", "provider": "openai"})
        provider = OpenAIProvider(spec, env=env)
        provider._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    400, json={"error": {"message": "bad token tok-abcdefgh"}}
                )
            )
        )
        messages = render_messages([Message(role="user", content="hi")], Case(), tmp_path)

        async def go():
            try:
                await provider.complete(CompletionRequest(model=spec, messages=messages))
            finally:
                await provider.aclose()

        with pytest.raises(ProviderError) as exc_info:
            asyncio.run(go())
        assert "tok-abcdefgh" not in str(exc_info.value)
        assert "<redacted>" in str(exc_info.value)

    def test_command_provider_receives_secrets(self, tmp_path):
        import sys

        script = tmp_path / "show_env.py"
        script.write_text(
            "import os, sys, json\n"
            "sys.stdin.read()\n"
            "print(os.environ.get('MY_SERVICE_KEY', 'MISSING'), end='')\n"
        )
        write_secrets(tmp_path / PROJECT_SECRETS_NAME, "MY_SERVICE_KEY: from-secrets\n")
        config = make_config(
            tmp_path,
            models=[
                {
                    "id": "scripted",
                    "provider": "command",
                    "command": f"{sys.executable} {script}",
                }
            ],
            cases=[{"id": "c1", "vars": {"q": "x"}}],
        )
        result = run_eval(config, make_settings(tmp_path))
        assert result.records[0].output == "from-secrets"


class TestRunIntegration:
    @posix_only
    def test_loose_permissions_surface_as_a_run_warning(self, tmp_path):
        write_secrets(tmp_path / PROJECT_SECRETS_NAME, "K: v\n", mode=0o644)
        result = run_eval(make_config(tmp_path), make_settings(tmp_path))
        assert any("readable by other users" in w for w in result.warnings)


def test_redact_helper():
    assert redact("key is sk-12345678 here", ["sk-12345678"]) == "key is <redacted> here"
    assert redact("nothing", []) == "nothing"
    # too short to redact safely (would mangle unrelated text)
    assert redact("abc appears", ["abc"]) == "abc appears"
