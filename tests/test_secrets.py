"""Secrets come from a gitignored file; the environment always wins."""

import asyncio
import os
from pathlib import Path

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
    candidate_paths,
    describe_secrets,
    load_secrets,
    redact,
    user_secrets_path,
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
            # Reports *whether* it got the value, not the value: echoing a
            # credential is exactly what the output scrub now removes, so a
            # test that checks for it verifies the scrub instead.
            "import os, sys, json\n"
            "sys.stdin.read()\n"
            "got = os.environ.get('MY_SERVICE_KEY')\n"
            "print('MATCHED' if got == 'from-secrets' else f'MISSING:{got!r}', end='')\n"
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
        assert result.records[0].output == "MATCHED"


class TestRunIntegration:
    @posix_only
    def test_loose_permissions_surface_as_a_run_warning(self, tmp_path):
        write_secrets(tmp_path / PROJECT_SECRETS_NAME, "K: v\n", mode=0o644)
        result = run_eval(make_config(tmp_path), make_settings(tmp_path))
        assert any("readable by other users" in w for w in result.warnings)


class TestEveryCandidateIsConsidered:
    """A missing file is skipped, not a reason to stop looking.

    Mutation testing turned the `continue` into a `break` and nothing
    noticed — which would mean a project with no `.evaling.secrets.yaml`
    silently never reads `~/.config/evaling/secrets.yaml` either. Every
    existing test put the file in the first place searched, so no test ever
    walked past a missing candidate.
    """

    def user_file_at(self, monkeypatch, path):
        """Point the last candidate — the user config — at a temp file."""
        monkeypatch.setattr("evaling.secrets.user_secrets_path", lambda: path)

    def test_a_later_file_is_found_when_an_earlier_one_is_absent(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        project.mkdir()
        assert not (project / PROJECT_SECRETS_NAME).exists(), "the earlier candidate must be absent"
        self.user_file_at(monkeypatch, write_secrets(tmp_path / "user.yaml", "LATER: found-it\n"))
        values, _ = load_secrets(project)
        assert values.get("LATER") == "found-it", "the search stopped at the missing file"

    def test_an_earlier_file_takes_precedence(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        project.mkdir()
        write_secrets(project / PROJECT_SECRETS_NAME, "SHARED: from-project\n")
        self.user_file_at(monkeypatch, write_secrets(tmp_path / "user.yaml", "SHARED: from-user\n"))
        values, _ = load_secrets(project)
        assert values["SHARED"] == "from-project", "the project file is searched first"

    def test_keys_from_both_are_merged(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        project.mkdir()
        write_secrets(project / PROJECT_SECRETS_NAME, "ONLY_PROJECT: a\n")
        self.user_file_at(monkeypatch, write_secrets(tmp_path / "user.yaml", "ONLY_USER: b\n"))
        values, _ = load_secrets(project)
        assert values == {"ONLY_PROJECT": "a", "ONLY_USER": "b"}


class TestOneBadEntryDoesNotHideTheRest:
    def test_a_valueless_key_is_skipped_and_the_rest_load(self, tmp_path):
        path = write_secrets(
            tmp_path / PROJECT_SECRETS_NAME,
            "EMPTY_ONE:\nREAL_KEY: real-value\nANOTHER: second-value\n",
        )
        values, _ = load_secrets(path.parent)
        assert "EMPTY_ONE" not in values
        assert values["REAL_KEY"] == "real-value"
        assert values["ANOTHER"] == "second-value", "a skipped entry stopped the loop"


class TestWhatTheMessagesSay:
    def test_a_missing_explicit_file_names_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EVALING_SECRETS", str(tmp_path / "nowhere.yaml"))
        with pytest.raises(SecretsError, match="nowhere.yaml"):
            load_secrets(tmp_path)

    def test_a_non_mapping_names_the_type_it_actually_found(self, tmp_path):
        write_secrets(tmp_path / PROJECT_SECRETS_NAME, "- just\n- a list\n")
        with pytest.raises(SecretsError, match="got list"):
            load_secrets(tmp_path)

    def test_a_nested_value_names_the_type_it_actually_found(self, tmp_path):
        write_secrets(tmp_path / PROJECT_SECRETS_NAME, "KEY:\n  nested: mapping\n")
        with pytest.raises(SecretsError, match="got dict"):
            load_secrets(tmp_path)


class TestTheRedactionLengthFloor:
    """Below eight characters a "secret" mangles ordinary text."""

    def test_a_value_of_exactly_eight_is_redacted(self):
        assert redact("the key is abcdefgh here", ["abcdefgh"]) == "the key is <redacted> here"

    def test_a_value_of_seven_is_left_alone(self):
        assert redact("the word abcdefg here", ["abcdefg"]) == "the word abcdefg here"


class TestTheUserConfigLocation:
    """The documented fallback path, spelled exactly.

    Nothing asserted it, so mutation testing could rename it freely — and a
    wrong path here is silent: no error, just a secrets file that is never
    found and keys that appear to be missing.
    """

    def test_it_is_the_documented_location(self):
        assert user_secrets_path() == Path("~/.config/evaling/secrets.yaml").expanduser()

    def test_it_is_the_last_candidate_consulted(self):
        # Through the module, not the name imported here: conftest points the
        # user path at a nonexistent file for the whole suite, and this claim
        # is about ordering rather than about which path it is.
        import evaling.secrets

        assert candidate_paths(Path("/project"))[-1] == evaling.secrets.user_secrets_path()


class TestDescribeSecretsUsesTheEnvironmentItIsGiven:
    """`doctor` passes an environment; ignoring it would name the wrong file.

    A diagnostic that reports a different secrets file from the one actually
    loaded is worse than no diagnostic.
    """

    def test_an_explicit_file_is_described(self, tmp_path):
        explicit = write_secrets(tmp_path / "explicit.yaml", "FROM_EXPLICIT: v\n")
        described = describe_secrets(tmp_path, {"EVALING_SECRETS": str(explicit)})
        assert str(explicit) in [entry["path"] for entry in described]

    def test_its_variable_names_are_reported_and_its_values_are_not(self, tmp_path):
        explicit = write_secrets(tmp_path / "explicit.yaml", "FROM_EXPLICIT: super-secret\n")
        described = describe_secrets(tmp_path, {"EVALING_SECRETS": str(explicit)})
        [entry] = [e for e in described if e["path"] == str(explicit)]
        assert entry["keys"] == ["FROM_EXPLICIT"]
        assert "super-secret" not in str(entry)

    def test_an_absent_file_is_skipped_without_stopping_the_walk(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        project.mkdir()
        user = write_secrets(tmp_path / "user.yaml", "FROM_USER: v\n")
        monkeypatch.setattr("evaling.secrets.user_secrets_path", lambda: user)
        described = describe_secrets(project)
        assert [entry["keys"] for entry in described] == [["FROM_USER"]]
