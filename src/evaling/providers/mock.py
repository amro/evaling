"""Deterministic mock provider for tests and dry runs.

Behavior is controlled by the model's ``params``:

- ``response``: return this fixed string.
- (default) echo the last user message's text, with each media part appended
  as ``[<kind>:<sha256 prefix>]`` so tests can verify media plumbing.
- ``fail_times``: fail the first N calls with a retryable error.
- ``error: fatal``: always fail with a non-retryable error.
- ``cost``: report this cost (USD) per call (default 0).

Token usage is a deterministic function of the text (length // 4).
"""

import asyncio

from evaling.content import MediaRef
from evaling.providers.base import Completion, CompletionRequest, Provider, ProviderError
from evaling.render import RenderedText


class MockProvider(Provider):
    SUPPORTED_MEDIA = frozenset({"image", "file", "audio", "video"})

    def __init__(self, spec):
        super().__init__(spec)
        self._calls = 0

    async def complete(self, request: CompletionRequest) -> Completion:
        # Yield to the event loop so tests exercise real interleaving, like an
        # HTTP provider would. Note ``fail_times`` counts calls per model
        # instance across ALL cells sharing that model, not per case.
        await asyncio.sleep(0)
        params = self.spec.params
        self._calls += 1

        if params.get("error") == "fatal":
            raise ProviderError("mock fatal error", retryable=False)
        if self._calls <= int(params.get("fail_times", 0)):
            raise ProviderError(f"mock transient failure #{self._calls}", retryable=True)

        if "response" in params:  # noqa: SIM108 - clearer than a 100-char ternary
            text = str(params["response"])
        else:
            text = _echo_last_user_message(request)

        prompt_chars = sum(len(m.text) for m in request.messages)
        return Completion(
            text=text,
            input_tokens=prompt_chars // 4,
            output_tokens=len(text) // 4,
            cost_usd=float(params.get("cost", 0.0)),
            raw={"mock_calls": self._calls},
        )


def _echo_last_user_message(request: CompletionRequest) -> str:
    for message in reversed(request.messages):
        if message.role != "user":
            continue
        pieces = []
        for part in message.parts:
            if isinstance(part, RenderedText):
                pieces.append(part.text)
            elif isinstance(part, MediaRef):
                pieces.append(f"[{part.kind}:{part.sha256[:8]}]")
        return " ".join(piece for piece in pieces if piece)
    return "mock response"
