"""Run any CLI or script as a model.

The request is written to stdin as JSON; stdout is the response. This makes
anything you can invoke from a shell evaluable — local inference binaries,
agent harnesses, wrapper scripts around unsupported APIs.

stdout may be either plain text (used verbatim) or a JSON object, which lets a
script report usage too::

    {"text": "...", "input_tokens": 12, "output_tokens": 34, "cost_usd": 0.001}
"""

import asyncio
import json
from typing import Any

from evaling.providers.base import Completion, CompletionRequest, Provider, ProviderError
from evaling.providers.pricing import estimate_cost
from evaling.storage import serialize_messages

DEFAULT_TIMEOUT_S = 300.0


class CommandProvider(Provider):
    """Shell out to ``command``: request JSON on stdin, response on stdout."""

    SUPPORTED_MEDIA = frozenset({"image", "file", "audio", "video"})

    async def complete(self, request: CompletionRequest) -> Completion:
        payload = json.dumps(
            {
                "model": self.spec.params.get("model", self.spec.id),
                "params": {k: v for k, v in self.spec.params.items() if k != "pricing"},
                # Media parts carry their path and content hash, so a script can
                # read the files it needs.
                "messages": serialize_messages(request.messages),
            }
        )
        stdout, stderr, code = await self._run(payload)

        if code != 0:
            detail = stderr.strip() or stdout.strip() or "<no output>"
            raise ProviderError(
                f"model {self.spec.id!r}: command exited {code}: {detail[:300]}",
                # A failing script is usually deterministic, but transient
                # causes (a busy GPU, a flaky network call inside the script)
                # are common enough that a retry is worth one attempt.
                retryable=True,
            )
        return self._completion(stdout)

    async def _run(self, payload: str) -> tuple[str, str, int]:
        timeout = self.spec.timeout_s or DEFAULT_TIMEOUT_S
        process = await asyncio.create_subprocess_shell(
            self.spec.command or "",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                process.communicate(payload.encode()), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ProviderError(
                f"model {self.spec.id!r}: command timed out after {timeout}s", retryable=True
            ) from exc
        return out.decode(errors="replace"), err.decode(errors="replace"), process.returncode

    def _completion(self, stdout: str) -> Completion:
        text = stdout
        usage: dict[str, Any] = {}
        stripped = stdout.strip()
        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict) and "text" in data:
                text = str(data["text"])
                usage = data

        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        cost = usage.get("cost_usd")
        if cost is None:
            cost = estimate_cost(
                str(self.spec.params.get("model", self.spec.id)),
                input_tokens,
                output_tokens,
                self.spec.params,
            )
        return Completion(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            raw={"exit_code": 0},
        )
