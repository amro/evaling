"""HTTP providers tested against a faked transport — never the network."""

import asyncio
import base64
import json

import httpx
import pytest

from evaling.config import Case, Message, ModelSpec
from evaling.providers.anthropic import AnthropicProvider
from evaling.providers.base import CompletionRequest, ProviderError
from evaling.providers.openai import OpenAICompatibleProvider, OpenAIProvider
from evaling.render import render_messages


def build(provider_cls, spec_data, env=None):
    spec = ModelSpec.model_validate(spec_data)
    provider = provider_cls(spec, env=env if env is not None else {})
    return provider


def install(provider, handler):
    """Point the provider's client at a fake transport and capture requests."""
    seen = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return seen


def make_request(spec, messages=None, tmp_path=None, case=None):
    messages = messages or [Message(role="user", content="hello")]
    rendered = render_messages(messages, case or Case(), tmp_path or ".")
    return CompletionRequest(model=spec, messages=rendered)


def run(provider, request):
    async def go():
        try:
            return await provider.complete(request)
        finally:
            await provider.aclose()

    return asyncio.run(go())


def json_response(payload, status=200):
    return lambda request: httpx.Response(status, json=payload)


ANTHROPIC_OK = {
    "content": [{"type": "text", "text": "the answer"}],
    "usage": {"input_tokens": 100, "output_tokens": 50},
    "stop_reason": "end_turn",
    "model": "claude-sonnet-5",
}

OPENAI_OK = {
    "choices": [
        {"message": {"role": "assistant", "content": "the answer"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    "model": "gpt-5.2",
}


class TestAnthropic:
    def test_request_shape_and_usage(self, tmp_path):
        provider = build(
            AnthropicProvider,
            {"id": "claude-sonnet-5", "provider": "anthropic", "params": {"max_tokens": 512}},
            env={"ANTHROPIC_API_KEY": "sk-test"},
        )
        seen = install(provider, json_response(ANTHROPIC_OK))
        messages = [
            Message(role="system", content="Be brief."),
            Message(role="user", content="hello"),
        ]
        completion = run(provider, make_request(provider.spec, messages, tmp_path))

        [request] = seen
        assert str(request.url) == "https://api.anthropic.com/v1/messages"
        assert request.headers["x-api-key"] == "sk-test"
        assert request.headers["anthropic-version"] == "2023-06-01"
        body = json.loads(request.content)
        assert body["model"] == "claude-sonnet-5"
        assert body["max_tokens"] == 512
        assert body["system"] == "Be brief."  # system is out of band
        assert body["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]}
        ]

        assert completion.text == "the answer"
        assert completion.input_tokens == 100
        assert completion.output_tokens == 50
        # 100 in @ $3/Mtok + 50 out @ $15/Mtok
        assert completion.cost_usd == pytest.approx(0.00105)

    def test_default_max_tokens_supplied(self, tmp_path):
        provider = build(
            AnthropicProvider,
            {"id": "claude-opus-5", "provider": "anthropic"},
            env={"ANTHROPIC_API_KEY": "k"},
        )
        seen = install(provider, json_response(ANTHROPIC_OK))
        run(provider, make_request(provider.spec, tmp_path=tmp_path))
        assert json.loads(seen[0].content)["max_tokens"] == 4096

    def test_multimodal_blocks(self, tmp_path):
        (tmp_path / "pic.png").write_bytes(b"PNGDATA")
        (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")
        provider = build(
            AnthropicProvider,
            {"id": "claude-opus-5", "provider": "anthropic"},
            env={"ANTHROPIC_API_KEY": "k"},
        )
        seen = install(provider, json_response(ANTHROPIC_OK))
        messages = [
            Message.model_validate(
                {
                    "role": "user",
                    "content": [{"text": "look"}, {"image": "pic.png"}, {"file": "doc.pdf"}],
                }
            )
        ]
        run(provider, make_request(provider.spec, messages, tmp_path))

        blocks = json.loads(seen[0].content)["messages"][0]["content"]
        assert blocks[0] == {"type": "text", "text": "look"}
        assert blocks[1]["type"] == "image"
        assert blocks[1]["source"]["media_type"] == "image/png"
        assert base64.standard_b64decode(blocks[1]["source"]["data"]) == b"PNGDATA"
        assert blocks[2]["type"] == "document"  # PDFs ride as documents
        assert blocks[2]["source"]["media_type"] == "application/pdf"

    def test_refusal_is_a_clear_error(self, tmp_path):
        provider = build(
            AnthropicProvider,
            {"id": "claude-opus-5", "provider": "anthropic"},
            env={"ANTHROPIC_API_KEY": "k"},
        )
        payload = {"content": [], "stop_reason": "refusal", "stop_details": {"category": "cyber"}}
        install(provider, json_response(payload))
        with pytest.raises(ProviderError, match="declined this request.*cyber") as exc_info:
            run(provider, make_request(provider.spec, tmp_path=tmp_path))
        assert exc_info.value.retryable is False

    def test_base_url_override(self, tmp_path):
        provider = build(
            AnthropicProvider,
            {
                "id": "claude-opus-5",
                "provider": "anthropic",
                "base_url": "https://proxy.internal/anthropic/",
            },
            env={"ANTHROPIC_API_KEY": "k"},
        )
        seen = install(provider, json_response(ANTHROPIC_OK))
        run(provider, make_request(provider.spec, tmp_path=tmp_path))
        assert str(seen[0].url) == "https://proxy.internal/anthropic/v1/messages"


class TestOpenAI:
    def test_request_shape_and_usage(self, tmp_path):
        provider = build(
            OpenAIProvider,
            {"id": "gpt-5.2", "provider": "openai", "params": {"temperature": 0.2}},
            env={"OPENAI_API_KEY": "sk-openai"},
        )
        seen = install(provider, json_response(OPENAI_OK))
        messages = [
            Message(role="system", content="Be brief."),
            Message(role="user", content="hello"),
        ]
        completion = run(provider, make_request(provider.spec, messages, tmp_path))

        [request] = seen
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer sk-openai"
        body = json.loads(request.content)
        assert body["temperature"] == 0.2
        # system stays a message; text-only turns send plain strings
        assert body["messages"] == [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "hello"},
        ]
        assert completion.text == "the answer"
        assert completion.input_tokens == 100
        assert completion.cost_usd is None  # no built-in pricing for this model

    def test_pricing_from_config(self, tmp_path):
        provider = build(
            OpenAIProvider,
            {
                "id": "gpt-5.2",
                "provider": "openai",
                "params": {"pricing": {"input": 1.25, "output": 10.0}},
            },
            env={"OPENAI_API_KEY": "k"},
        )
        seen = install(provider, json_response(OPENAI_OK))
        completion = run(provider, make_request(provider.spec, tmp_path=tmp_path))
        # pricing is evaling's own key — it must not leak into the API payload
        assert "pricing" not in json.loads(seen[0].content)
        assert completion.cost_usd == pytest.approx(100 * 1.25e-6 + 50 * 10e-6)

    def test_multimodal_parts(self, tmp_path):
        (tmp_path / "pic.png").write_bytes(b"PNG")
        (tmp_path / "clip.mp3").write_bytes(b"MP3")
        provider = build(
            OpenAIProvider, {"id": "gpt-5.2", "provider": "openai"}, env={"OPENAI_API_KEY": "k"}
        )
        seen = install(provider, json_response(OPENAI_OK))
        messages = [
            Message.model_validate(
                {
                    "role": "user",
                    "content": [{"text": "hi"}, {"image": "pic.png"}, {"audio": "clip.mp3"}],
                }
            )
        ]
        run(provider, make_request(provider.spec, messages, tmp_path))

        parts = json.loads(seen[0].content)["messages"][0]["content"]
        assert parts[0] == {"type": "text", "text": "hi"}
        assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert parts[2]["type"] == "input_audio"
        assert parts[2]["input_audio"]["format"] == "mp3"

    def test_model_param_overrides_id(self, tmp_path):
        provider = build(
            OpenAIProvider,
            {"id": "cheap-tier", "provider": "openai", "params": {"model": "gpt-5.2-mini"}},
            env={"OPENAI_API_KEY": "k"},
        )
        seen = install(provider, json_response(OPENAI_OK))
        run(provider, make_request(provider.spec, tmp_path=tmp_path))
        assert json.loads(seen[0].content)["model"] == "gpt-5.2-mini"


class TestOpenAICompatible:
    def test_local_server_without_key(self, tmp_path):
        provider = build(
            OpenAICompatibleProvider,
            {
                "id": "llama3.1:8b",
                "provider": "openai-compatible",
                "base_url": "http://localhost:11434/v1",
            },
            env={},
        )
        seen = install(provider, json_response(OPENAI_OK))
        run(provider, make_request(provider.spec, tmp_path=tmp_path))
        [request] = seen
        assert str(request.url) == "http://localhost:11434/v1/chat/completions"
        assert "authorization" not in request.headers  # no key needed locally

    def test_custom_api_key_env(self, tmp_path):
        provider = build(
            OpenAICompatibleProvider,
            {
                "id": "gemini-2.5-flash",
                "provider": "openai-compatible",
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "api_key_env": "GEMINI_API_KEY",
            },
            env={"GEMINI_API_KEY": "gem-key"},
        )
        seen = install(provider, json_response(OPENAI_OK))
        run(provider, make_request(provider.spec, tmp_path=tmp_path))
        assert seen[0].headers["authorization"] == "Bearer gem-key"

    def test_list_content_response_joined(self, tmp_path):
        provider = build(
            OpenAICompatibleProvider,
            {"id": "m", "provider": "openai-compatible", "base_url": "http://localhost:8000/v1"},
            env={},
        )
        payload = {
            "choices": [{"message": {"content": [{"type": "text", "text": "chunked"}]}}],
            "usage": {},
        }
        install(provider, json_response(payload))
        assert run(provider, make_request(provider.spec, tmp_path=tmp_path)).text == "chunked"


class TestErrorMapping:
    @pytest.mark.parametrize(
        "status,retryable",
        [(429, True), (500, True), (503, True), (408, True), (400, False), (404, False)],
    )
    def test_status_retryability(self, tmp_path, status, retryable):
        provider = build(
            OpenAIProvider, {"id": "gpt-5.2", "provider": "openai"}, env={"OPENAI_API_KEY": "k"}
        )
        install(provider, json_response({"error": {"message": "boom"}}, status=status))
        with pytest.raises(ProviderError, match=f"HTTP {status}.*boom") as exc_info:
            run(provider, make_request(provider.spec, tmp_path=tmp_path))
        assert exc_info.value.retryable is retryable

    def test_auth_error_names_the_env_var(self, tmp_path):
        provider = build(
            OpenAIProvider, {"id": "gpt-5.2", "provider": "openai"}, env={"OPENAI_API_KEY": "bad"}
        )
        install(provider, json_response({"error": {"message": "bad key"}}, status=401))
        with pytest.raises(ProviderError, match="check OPENAI_API_KEY"):
            run(provider, make_request(provider.spec, tmp_path=tmp_path))

    def test_missing_key_fails_before_any_request(self, tmp_path):
        provider = build(AnthropicProvider, {"id": "m", "provider": "anthropic"}, env={})
        seen = install(provider, json_response(ANTHROPIC_OK))
        with pytest.raises(ProviderError, match="no API key found — set ANTHROPIC_API_KEY"):
            run(provider, make_request(provider.spec, tmp_path=tmp_path))
        assert seen == []

    def test_timeout_is_retryable(self, tmp_path):
        provider = build(
            OpenAIProvider,
            {"id": "gpt-5.2", "provider": "openai", "timeout_s": 3},
            env={"OPENAI_API_KEY": "k"},
        )

        def timeout(request):
            raise httpx.ReadTimeout("too slow", request=request)

        install(provider, timeout)
        with pytest.raises(ProviderError, match="timed out after 3") as exc_info:
            run(provider, make_request(provider.spec, tmp_path=tmp_path))
        assert exc_info.value.retryable is True

    def test_connection_error_is_retryable(self, tmp_path):
        provider = build(
            OpenAICompatibleProvider,
            {"id": "m", "provider": "openai-compatible", "base_url": "http://localhost:9/v1"},
            env={},
        )

        def refused(request):
            raise httpx.ConnectError("connection refused", request=request)

        install(provider, refused)
        with pytest.raises(ProviderError, match="connection error") as exc_info:
            run(provider, make_request(provider.spec, tmp_path=tmp_path))
        assert exc_info.value.retryable is True

    def test_non_json_body_is_fatal(self, tmp_path):
        provider = build(
            OpenAIProvider, {"id": "gpt-5.2", "provider": "openai"}, env={"OPENAI_API_KEY": "k"}
        )
        install(provider, lambda request: httpx.Response(200, text="<html>gateway</html>"))
        with pytest.raises(ProviderError, match="was not JSON") as exc_info:
            run(provider, make_request(provider.spec, tmp_path=tmp_path))
        assert exc_info.value.retryable is False


def test_timeout_s_reaches_the_client():
    provider = build(
        OpenAIProvider,
        {"id": "gpt-5.2", "provider": "openai", "timeout_s": 12.5},
        env={"OPENAI_API_KEY": "k"},
    )
    assert provider.client().timeout.read == 12.5
    asyncio.run(provider.aclose())
    assert provider._client is None
