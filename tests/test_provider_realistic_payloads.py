"""Providers parsed against full, realistic API response shapes.

The unit tests use minimal payloads; these use complete ones with the fields
real APIs actually send (extra top-level keys, multiple content blocks,
thinking/refusal blocks, cache-token fields). A provider must pick out what it
needs and ignore the rest, so a vendor adding a field doesn't break a run.
"""

import asyncio
import json

import httpx
import pytest

from evaling.config import Case, Message, ModelSpec
from evaling.providers.anthropic import AnthropicProvider
from evaling.providers.base import CompletionRequest
from evaling.providers.openai import OpenAIProvider
from evaling.render import render_messages

# A full Anthropic Messages response, including fields evaling ignores.
ANTHROPIC_FULL = {
    "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "abc"},
        {"type": "text", "text": "The Treaty of Ghent ended the War of 1812."},
    ],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 2095,
        "output_tokens": 503,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 1024,
        "service_tier": "standard",
    },
}

# A full OpenAI chat-completion response.
OPENAI_FULL = {
    "id": "chatcmpl-B9MHDbslfkBeAs8l4bebGdFOJ6PeG",
    "object": "chat.completion",
    "created": 1741570283,
    "model": "gpt-5.2",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "The Treaty of Ghent ended the War of 1812.",
                "refusal": None,
                "annotations": [],
            },
            "logprobs": None,
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 2095,
        "completion_tokens": 503,
        "total_tokens": 2598,
        "prompt_tokens_details": {"cached_tokens": 1024, "audio_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 0, "audio_tokens": 0},
    },
    "service_tier": "default",
    "system_fingerprint": "fp_fc9f1d7035",
}


def call(provider_cls, spec_data, payload, tmp_path, env=None):
    spec = ModelSpec.model_validate(spec_data)
    provider = provider_cls(spec, env=env or {"ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k"})
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    messages = render_messages([Message(role="user", content="q")], Case(), tmp_path)

    async def go():
        try:
            return await provider.complete(CompletionRequest(model=spec, messages=messages))
        finally:
            await provider.aclose()

    return asyncio.run(go())


class TestAnthropicRealisticShapes:
    def test_full_response(self, tmp_path):
        completion = call(
            AnthropicProvider,
            {"id": "claude-sonnet-5", "provider": "anthropic"},
            ANTHROPIC_FULL,
            tmp_path,
        )
        # thinking blocks are skipped; only text is the answer
        assert completion.text == "The Treaty of Ghent ended the War of 1812."
        assert completion.input_tokens == 2095
        assert completion.output_tokens == 503
        assert completion.cost_usd == pytest.approx(2095 * 2e-6 + 503 * 10e-6)

    def test_multiple_text_blocks_are_joined(self, tmp_path):
        payload = dict(ANTHROPIC_FULL)
        payload["content"] = [
            {"type": "text", "text": "part one. "},
            {"type": "text", "text": "part two."},
        ]
        completion = call(
            AnthropicProvider, {"id": "claude-opus-5", "provider": "anthropic"}, payload, tmp_path
        )
        assert completion.text == "part one. part two."

    def test_unknown_block_types_ignored(self, tmp_path):
        payload = dict(ANTHROPIC_FULL)
        payload["content"] = [
            {"type": "some_future_block", "data": {"x": 1}},
            {"type": "text", "text": "still readable"},
        ]
        completion = call(
            AnthropicProvider, {"id": "claude-opus-5", "provider": "anthropic"}, payload, tmp_path
        )
        assert completion.text == "still readable"

    def test_missing_usage_is_tolerated(self, tmp_path):
        payload = {k: v for k, v in ANTHROPIC_FULL.items() if k != "usage"}
        completion = call(
            AnthropicProvider, {"id": "claude-opus-5", "provider": "anthropic"}, payload, tmp_path
        )
        assert completion.input_tokens is None
        assert completion.cost_usd is None  # unknown, not free


class TestOpenAIRealisticShapes:
    def test_full_response(self, tmp_path):
        completion = call(
            OpenAIProvider, {"id": "gpt-5.2", "provider": "openai"}, OPENAI_FULL, tmp_path
        )
        assert completion.text == "The Treaty of Ghent ended the War of 1812."
        assert completion.input_tokens == 2095
        assert completion.output_tokens == 503
        assert completion.raw["finish_reason"] == "stop"

    def test_null_content_does_not_crash(self, tmp_path):
        # e.g. a refusal or a tool-call-only turn
        payload = json.loads(json.dumps(OPENAI_FULL))
        payload["choices"][0]["message"]["content"] = None
        payload["choices"][0]["message"]["refusal"] = "I can't help with that."
        completion = call(
            OpenAIProvider, {"id": "gpt-5.2", "provider": "openai"}, payload, tmp_path
        )
        assert completion.text == ""

    def test_empty_choices_does_not_crash(self, tmp_path):
        payload = json.loads(json.dumps(OPENAI_FULL))
        payload["choices"] = []
        completion = call(
            OpenAIProvider, {"id": "gpt-5.2", "provider": "openai"}, payload, tmp_path
        )
        assert completion.text == ""

    def test_missing_usage_is_tolerated(self, tmp_path):
        payload = {k: v for k, v in OPENAI_FULL.items() if k != "usage"}
        completion = call(
            OpenAIProvider, {"id": "gpt-5.2", "provider": "openai"}, payload, tmp_path
        )
        assert completion.input_tokens is None
