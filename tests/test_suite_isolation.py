"""The suite's own guarantees, tested.

"Tests never touch the network, and never use a real key" was a line in
CONTRIBUTING.md that nothing enforced. It was already false: one test ran a
full eval against an `openai-compatible` model and really did connect to the
host in its `base_url`, and nothing stopped a contributor's exported
`ANTHROPIC_API_KEY` from being spent by the suite.

`tests/conftest.py` now enforces both. These tests are what keeps the
enforcement honest — delete either fixture and they fail.
"""

import os
from pathlib import Path

import httpx
import pytest

from conftest import CANARY_VAR, CREDENTIAL_SUFFIXES
from evaling.config.schema import ModelSpec
from evaling.providers.anthropic import AnthropicProvider
from evaling.providers.http import ProviderError


class TestNoTestReachesTheNetwork:
    async def request(self, client):
        return await client.get("https://unreachable.invalid/v1/messages")

    def test_a_client_with_no_transport_refuses_to_send(self):
        import asyncio

        async def go():
            async with httpx.AsyncClient() as client:
                await self.request(client)

        with pytest.raises(BaseException, match="never touch the network"):
            asyncio.run(go())

    def test_the_sync_client_refuses_too(self):
        with httpx.Client() as client, pytest.raises(BaseException, match="never touch"):
            client.get("https://unreachable.invalid/v1/messages")

    def test_the_refusal_names_the_host_a_test_tried_to_reach(self):
        with (
            httpx.Client() as client,
            pytest.raises(BaseException, match="example.invalid"),
        ):
            client.get("https://example.invalid/v1")

    def test_the_refusal_is_not_an_ordinary_exception(self):
        """It has to escape `except Exception`, which the engine uses per cell.

        Asserted on the raised object rather than by importing the class:
        mutmut runs the suite from a copied tree where `conftest` is a second
        module object. Loosening the raises() above to BaseException was not
        enough on its own — RuntimeError satisfies that too.
        """
        with (
            pytest.raises(BaseException) as caught,  # noqa: B017, PT011
            httpx.Client() as client,
        ):
            client.get("https://unreachable.invalid/v1")
        assert not isinstance(caught.value, Exception), (
            "a refusal that is an Exception gets swallowed by the engine's "
            "per-cell handler and the run goes green"
        )

    def test_a_run_surfaces_a_refusal_instead_of_recording_it(self, tmp_path):
        """The scenario the BaseException exists for, end to end.

        `run_eval` catches Exception per cell so one provider failure cannot
        lose a whole run. A test that accidentally reaches the network through
        a provider would therefore have produced a cell error and a green
        run — prevented, but silent.
        """
        from evaling.engine import run_eval
        from helpers import make_config, make_settings

        config = make_config(
            tmp_path,
            models=[
                {"id": "m1", "provider": "openai-compatible", "base_url": "http://x.invalid/v1"}
            ],
        )
        with pytest.raises(BaseException) as caught:  # noqa: B017, PT011
            run_eval(config, make_settings(tmp_path))
        assert not isinstance(caught.value, Exception)

    def test_an_injected_transport_still_works(self):
        """The guard must not break the way every provider test stages a call."""
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
        with httpx.Client(transport=transport) as client:
            assert client.get("https://api.anthropic.com/v1/messages").json() == {"ok": True}


class TestNoTestSeesARealKey:
    def test_the_environment_carries_no_credential(self):
        leaked = [name for name in os.environ if name.endswith(CREDENTIAL_SUFFIXES)]
        assert leaked == [], f"a real credential reached the suite: {leaked}"

    def test_the_canary_is_stripped(self):
        """Proof the stripping runs, on a machine with nothing exported.

        The assertion above is vacuously true on every CI runner. A canary
        injected for the whole session gives it something to be about: delete
        the fixture and this fails everywhere, not just on a developer's
        machine with a key in their shell.
        """
        assert CANARY_VAR not in os.environ

    def test_the_user_secrets_file_is_not_consulted(self):
        """`~/.config/evaling/secrets.yaml` is a credential source too.

        Asserted as "not the real path" rather than "the file is absent":
        the latter passes on any machine that simply has no such file, which
        proves nothing about whether the suite would have read it.
        """
        import evaling.secrets

        real = Path("~/.config/evaling/secrets.yaml").expanduser()
        assert evaling.secrets.user_secrets_path() != real
        assert not evaling.secrets.user_secrets_path().exists()

    def test_a_provider_given_the_real_environment_finds_no_key(self):
        """The claim that matters, stated the way a provider would see it.

        `env=os.environ` is what the engine passes. On an unguarded machine
        with the variable exported this raises nothing and the next step is a
        billed request.
        """
        spec = ModelSpec.model_validate({"id": "m", "provider": "anthropic"})
        provider = AnthropicProvider(spec=spec, env=dict(os.environ))
        with pytest.raises(ProviderError, match="no API key found"):
            provider.api_key()

    def test_the_secrets_file_variable_is_cleared(self):
        assert "EVALING_SECRETS" not in os.environ
