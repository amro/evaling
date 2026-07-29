"""OpenAI chat-completions provider, and the generic OpenAI-compatible variant.

The same wire format serves OpenAI, Gemini's OpenAI-compatibility endpoint,
OpenRouter, Ollama, vLLM, LM Studio, and llama.cpp's server — which is why one
adapter covers the whole ecosystem.
"""

from typing import Any

from evaling.content import MediaRef
from evaling.providers.base import Completion, CompletionRequest
from evaling.providers.http import HttpProvider, b64_media
from evaling.providers.pricing import estimate_cost
from evaling.render import RenderedMessage, RenderedText

DEFAULT_BASE_URL = "https://api.openai.com/v1"

# OpenAI names audio formats by codec, not MIME type.
AUDIO_FORMATS = {
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/mp4": "m4a",
}


class OpenAIProvider(HttpProvider):
    """Calls {base_url}/chat/completions."""

    DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
    SUPPORTED_MEDIA = frozenset({"image", "file", "audio"})
    DEFAULT_BASE_URL = DEFAULT_BASE_URL

    async def complete(self, request: CompletionRequest) -> Completion:
        payload: dict[str, Any] = {
            "model": self.api_model,
            "messages": [await _message(message) for message in request.messages],
            **self.forwarded_params(),
        }
        base_url = (self.spec.base_url or self.DEFAULT_BASE_URL).rstrip("/")
        data = await self.post_json(
            f"{base_url}/chat/completions",
            headers=self._headers(),
            payload=payload,
        )

        choices = data.get("choices") or [{}]
        content = (choices[0].get("message") or {}).get("content")
        usage = data.get("usage") or {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        return Completion(
            text=content if isinstance(content, str) else _join_content(content),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=estimate_cost(self.api_model, input_tokens, output_tokens, self.spec.params),
            raw={
                "finish_reason": choices[0].get("finish_reason"),
                "model": data.get("model"),
            },
        )

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        key = self.api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers


class OpenAICompatibleProvider(OpenAIProvider):
    """Any endpoint speaking the OpenAI chat-completions format.

    ``base_url`` is required (enforced by the config schema). An API key is
    optional: local servers usually need none, while hosted backends set their
    own variable via ``api_key_env`` (e.g. GEMINI_API_KEY, OPENROUTER_API_KEY).
    """

    DEFAULT_API_KEY_ENV = ""
    REQUIRES_API_KEY = False


async def _message(message: RenderedMessage) -> dict[str, Any]:
    # Plain strings for text-only turns: many OpenAI-compatible servers reject
    # the content-parts array.
    if all(isinstance(part, RenderedText) for part in message.parts):
        return {"role": message.role, "content": message.text}
    return {"role": message.role, "content": [await _part(part) for part in message.parts]}


async def _part(part: Any) -> dict[str, Any]:
    if isinstance(part, RenderedText):
        return {"type": "text", "text": part.text}
    assert isinstance(part, MediaRef)
    data = await b64_media(part)
    if part.kind == "image":
        return {"type": "image_url", "image_url": {"url": _data_url(part, data)}}
    if part.kind == "audio":
        # Bare base64 plus a codec name — the audio API takes no data URL.
        return {
            "type": "input_audio",
            "input_audio": {
                "data": data,
                "format": AUDIO_FORMATS.get(part.media_type, part.path.suffix.lstrip(".")),
            },
        }
    return {
        "type": "file",
        "file": {"filename": part.path.name, "file_data": _data_url(part, data)},
    }


def _data_url(part: MediaRef, data: str) -> str:
    return f"data:{part.media_type};base64,{data}"


def _join_content(content: Any) -> str:
    """Some servers return content as a list of parts rather than a string."""
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return "" if content is None else str(content)
