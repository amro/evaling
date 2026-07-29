"""Anthropic Messages API provider."""

from typing import Any

from evaling.content import MediaRef
from evaling.providers.base import Completion, CompletionRequest, ProviderError
from evaling.providers.http import HttpProvider, b64_media
from evaling.providers.pricing import estimate_cost
from evaling.render import RenderedMessage, RenderedText

API_VERSION = "2023-06-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MAX_TOKENS = 4096


class AnthropicProvider(HttpProvider):
    """Calls /v1/messages. System turns become the top-level `system` field."""

    DEFAULT_API_KEY_ENV = "ANTHROPIC_API_KEY"
    SUPPORTED_MEDIA = frozenset({"image", "file"})

    async def complete(self, request: CompletionRequest) -> Completion:
        system, messages = await _split_system(request.messages)
        params = self.forwarded_params()
        payload: dict[str, Any] = {
            "model": self.api_model,
            "max_tokens": params.pop("max_tokens", DEFAULT_MAX_TOKENS),
            "messages": messages,
            **params,
        }
        if system:
            payload["system"] = system

        base_url = (self.spec.base_url or DEFAULT_BASE_URL).rstrip("/")
        data = await self.post_json(
            f"{base_url}/v1/messages",
            headers={
                "x-api-key": self.api_key() or "",
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            payload=payload,
        )

        if data.get("stop_reason") == "refusal":
            category = (data.get("stop_details") or {}).get("category")
            suffix = f" (category: {category})" if category else ""
            raise ProviderError(
                f"model {self.spec.id!r}: the model declined this request{suffix}",
                retryable=False,
            )

        usage = data.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        return Completion(
            text=_extract_text(data.get("content") or []),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=estimate_cost(self.api_model, input_tokens, output_tokens, self.spec.params),
            raw={"stop_reason": data.get("stop_reason"), "model": data.get("model")},
        )


async def _split_system(messages: list[RenderedMessage]) -> tuple[str, list[dict[str, Any]]]:
    """Anthropic takes system content out of band, not as a message role."""
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            system_parts.append(message.text)
            continue
        converted.append({"role": message.role, "content": await _content_blocks(message)})
    return "\n\n".join(part for part in system_parts if part), converted


async def _content_blocks(message: RenderedMessage) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, RenderedText):
            blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, MediaRef):
            blocks.append(await _media_block(part))
    return blocks


async def _media_block(ref: MediaRef) -> dict[str, Any]:
    source = {"type": "base64", "media_type": ref.media_type, "data": await b64_media(ref)}
    # PDFs ride as documents; images as images. Other kinds are rejected by
    # SUPPORTED_MEDIA before a request is ever built.
    block_type = "document" if ref.kind == "file" else "image"
    return {"type": block_type, "source": source}


def _extract_text(content: list[dict[str, Any]]) -> str:
    return "".join(block.get("text", "") for block in content if block.get("type") == "text")
