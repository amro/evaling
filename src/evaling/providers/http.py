"""Shared plumbing for HTTP-based providers.

One httpx client per provider instance, closed by ``aclose()`` at the end of a
run. Transport and status errors map to ProviderError with an accurate
``retryable`` flag so the engine's backoff only retries what is worth retrying.
"""

import base64
from typing import Any

import httpx

from evaling.config.schema import ModelSpec
from evaling.providers.base import Provider, ProviderError

DEFAULT_TIMEOUT_S = 120.0

# Params consumed by evaling itself — never forwarded to a provider API.
RESERVED_PARAMS = frozenset({"model", "pricing", "cost"})


class HttpProvider(Provider):
    """Base for providers that speak HTTP/JSON."""

    #: Environment variable consulted when the model spec sets no api_key_env.
    DEFAULT_API_KEY_ENV: str = ""
    #: Whether a missing API key is fatal (false for local/self-hosted servers).
    REQUIRES_API_KEY: bool = True

    def __init__(self, spec: ModelSpec, *, env: dict[str, str] | None = None):
        super().__init__(spec)
        import os

        self._env = os.environ if env is None else env
        self._client: httpx.AsyncClient | None = None

    # -- configuration ----------------------------------------------------

    @property
    def api_model(self) -> str:
        """The model name sent to the API (``params.model`` overrides the id)."""
        return str(self.spec.params.get("model", self.spec.id))

    @property
    def api_key_env(self) -> str:
        return self.spec.api_key_env or self.DEFAULT_API_KEY_ENV

    def api_key(self) -> str | None:
        key = self._env.get(self.api_key_env) if self.api_key_env else None
        if not key and self.REQUIRES_API_KEY:
            raise ProviderError(
                f"model {self.spec.id!r}: no API key found — set {self.api_key_env}",
                retryable=False,
            )
        return key or None

    def forwarded_params(self) -> dict[str, Any]:
        """Model params passed through to the API, minus evaling's own keys."""
        return {k: v for k, v in self.spec.params.items() if k not in RESERVED_PARAMS}

    @property
    def timeout_s(self) -> float:
        return self.spec.timeout_s or DEFAULT_TIMEOUT_S

    # -- transport --------------------------------------------------------

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def post_json(
        self, url: str, *, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            response = await self.client().post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"model {self.spec.id!r}: request timed out after {self.timeout_s}s",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"model {self.spec.id!r}: connection error: {exc}", retryable=True
            ) from exc

        if response.status_code >= 400:
            raise self._status_error(response)
        try:
            return response.json()
        except ValueError as exc:
            body = response.text[:200]
            raise ProviderError(
                f"model {self.spec.id!r}: response was not JSON: {body!r}", retryable=False
            ) from exc

    def _status_error(self, response: httpx.Response) -> ProviderError:
        status = response.status_code
        detail = self._redact(_error_detail(response))
        # 408/409/429 and 5xx are transient; other 4xx are caller errors.
        retryable = status in (408, 409, 429) or status >= 500
        hint = ""
        if status in (401, 403):
            hint = f" (check {self.api_key_env})" if self.api_key_env else ""
        return ProviderError(
            f"model {self.spec.id!r}: HTTP {status}{hint}: {detail}",
            retryable=retryable,
            retry_after=_retry_after(response) if retryable else None,
        )

    def _redact(self, text: str) -> str:
        """Never let a key reach an error message, a log, or results.jsonl.

        Nothing here echoes the key, but a misconfigured gateway that reflects
        request headers into its error body would otherwise persist it.
        """
        key = self._env.get(self.api_key_env) if self.api_key_env else None
        if key and len(key) >= 8 and key in text:
            return text.replace(key, "<redacted>")
        return text


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds from a Retry-After header, when the server sends a numeric one."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None  # HTTP-date form: fall back to exponential backoff
    return seconds if seconds >= 0 else None


def _error_detail(response: httpx.Response) -> str:
    """Best-effort human-readable message from an error response body."""
    try:
        data = response.json()
    except ValueError:
        return response.text[:300].strip() or "<empty body>"
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str):
            return error
        if data.get("message"):
            return str(data["message"])
    return str(data)[:300]


def b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")
