import asyncio

import pytest

from evaling.config import Case, Message, ModelSpec
from evaling.providers import Completion, CompletionRequest, ProviderError, create_provider
from evaling.render import render_messages


def make_request(params=None, messages=None, tmp_path=None):
    spec = ModelSpec(id="m", provider="mock", params=params or {})
    if messages is None:
        messages = [Message(role="user", content="hello there")]
    rendered = render_messages(messages, Case(), tmp_path or ".")
    return spec, CompletionRequest(model=spec, messages=rendered)


def complete(spec, request):
    provider = create_provider(spec)
    return asyncio.run(provider.complete(request))


def test_echoes_last_user_message(tmp_path):
    spec, request = make_request(tmp_path=tmp_path)
    completion = complete(spec, request)
    assert completion.text == "hello there"


def test_echo_picks_last_user_message(tmp_path):
    messages = [
        Message(role="user", content="first"),
        Message(role="assistant", content="draft"),
        Message(role="user", content="second"),
    ]
    spec, request = make_request(messages=messages, tmp_path=tmp_path)
    assert complete(spec, request).text == "second"


def test_fixed_response_param(tmp_path):
    spec, request = make_request(params={"response": "always this"}, tmp_path=tmp_path)
    assert complete(spec, request).text == "always this"


def test_echo_includes_media_hash_marker(tmp_path):
    (tmp_path / "dog.png").write_bytes(b"png")
    messages = [
        Message.model_validate(
            {"role": "user", "content": [{"text": "look:"}, {"image": "dog.png"}]}
        )
    ]
    spec, request = make_request(messages=messages, tmp_path=tmp_path)
    text = complete(spec, request).text
    assert text.startswith("look: [image:")
    assert len(text.split("[image:")[1].rstrip("]")) == 8


def test_deterministic_usage_and_cost(tmp_path):
    spec, request = make_request(tmp_path=tmp_path)
    first = complete(spec, request)
    second = complete(spec, request)
    assert first.input_tokens == second.input_tokens == len("hello there") // 4
    assert first.output_tokens == len("hello there") // 4
    assert first.cost_usd == 0.0


def test_fail_times_then_succeeds(tmp_path):
    spec, request = make_request(params={"fail_times": 2}, tmp_path=tmp_path)
    provider = create_provider(spec)

    async def run():
        failures = []
        for _ in range(2):
            with pytest.raises(ProviderError) as exc_info:
                await provider.complete(request)
            failures.append(exc_info.value)
        return failures, await provider.complete(request)

    failures, completion = asyncio.run(run())
    assert all(f.retryable for f in failures)
    assert isinstance(completion, Completion)


def test_fatal_error_not_retryable(tmp_path):
    spec, request = make_request(params={"error": "fatal"}, tmp_path=tmp_path)
    with pytest.raises(ProviderError) as exc_info:
        complete(spec, request)
    assert exc_info.value.retryable is False


def test_no_user_message_falls_back(tmp_path):
    messages = [Message(role="system", content="sys only")]
    spec, request = make_request(messages=messages, tmp_path=tmp_path)
    assert complete(spec, request).text == "mock response"


def test_every_schema_provider_is_registered():
    # Catches a provider added to the config schema but not to the registry:
    # the config would validate and then fail at run time.
    import typing

    from evaling.config.schema import ProviderName
    from evaling.providers import provider_class

    extras = {
        "openai-compatible": {"base_url": "http://localhost:1234/v1"},
        "command": {"command": "true"},
    }
    for name in typing.get_args(ProviderName):
        assert provider_class(name) is not None
        # and it constructs from a minimal valid spec
        spec = ModelSpec.model_validate({"id": "m", "provider": name, **extras.get(name, {})})
        assert create_provider(spec).spec.id == "m"


def test_unknown_provider_rejected():
    from evaling.providers import provider_class

    with pytest.raises(ProviderError, match="not implemented yet"):
        provider_class("telepathy")
