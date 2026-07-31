"""Run any CLI or script as a model.

The request is written to stdin as JSON; stdout is the response. This makes
anything you can invoke from a shell evaluable — local inference binaries,
agent harnesses, wrapper scripts around unsupported APIs.

stdout may be either plain text (used verbatim) or a JSON object, which lets a
script report usage too::

    {"text": "...", "input_tokens": 12, "output_tokens": 34, "cost_usd": 0.001}
"""

import asyncio
import contextlib
import json
import time
from typing import Any

from evaling.providers.base import Completion, CompletionRequest, Provider, ProviderError
from evaling.providers.pricing import estimate_cost
from evaling.secrets import redact
from evaling.storage import serialize_messages

DEFAULT_TIMEOUT_S = 300.0


def _release(process: "asyncio.subprocess.Process") -> None:
    """Close the subprocess transport while its event loop still exists.

    asyncio closes a subprocess transport only in its finalizer, so the pipes
    are released whenever the garbage collector happens to reach them. If that
    is after the loop has closed — which it is for anything that runs a loop
    per call — closing a pipe calls `loop.call_soon` on a dead loop and raises
    `RuntimeError: Event loop is closed` from inside `__del__`, where it can
    only become an unraisable exception. It is harmless to the run and
    invisible in normal use, but it turns up as noise in any test runner that
    reports unraisables, attributed to whichever test GC interrupted.

    `_transport` is private, hence the guard, but it is the only handle on the
    transport a `Process` offers and it has been there unchanged since 3.8.
    """
    transport = getattr(process, "_transport", None)
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.close()


class CommandProvider(Provider):
    """Shell out to ``command``: request JSON on stdin, response on stdout."""

    SUPPORTED_MEDIA = frozenset({"image", "file", "audio", "video"})

    def __init__(self, spec, *, env=None, base_dir=None, request_log=None):
        # Secrets reach the script through its environment — a wrapper around a
        # real API usually needs the same key evaling would have used.
        super().__init__(spec, env=env, base_dir=base_dir, request_log=request_log)
        #: Values to scrub from anything the script says back.
        self._secret_values = list(getattr(env, "secret_values", ()) or [])
        key_env = spec.api_key_env or ""
        if key_env and env is not None and env.get(key_env):
            self._secret_values.append(env[key_env])

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
        started = time.perf_counter()
        stdout, stderr, code = await self._run(payload)
        if self.request_log is not None:
            # A script's stderr is where its own diagnostics go, so it is the
            # useful half here — the analogue of a response body.
            self.request_log.record(
                model=self.spec.id,
                provider=self.spec.provider,
                command=self.spec.command,
                request=json.loads(payload),
                exit_code=code,
                stdout=stdout[:4000],
                stderr=stderr[:4000],
                elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            )

        if code != 0:
            # Redacted: the script runs with evaling's environment, so its own
            # diagnostics routinely echo the credential it was handed.
            detail = redact(stderr.strip() or stdout.strip() or "<no output>", self._secret_values)
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
            env=dict(self.env) if self.env is not None else None,
            # Run where the config lives, so `command: python3 score.py` means
            # the same thing as every other path in that config — and a config
            # is not silently dependent on the caller's working directory.
            cwd=str(self.base_dir) if self.base_dir else None,
        )
        try:
            out, err = await asyncio.wait_for(
                process.communicate(payload.encode()), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            # The child may have exited between the timeout and the kill.
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise ProviderError(
                f"model {self.spec.id!r}: command timed out after {timeout}s", retryable=True
            ) from exc
        except asyncio.CancelledError:
            # Cancellation (Ctrl-C, a failing sibling tearing down the run)
            # must not orphan the child: kill and reap it before propagating.
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise
        finally:
            _release(process)
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

        # Constructing first lets Completion validate the script's usage — a
        # junk value fails this cell cleanly instead of crashing the run's
        # totals — and estimate_cost then works from the coerced counts.
        completion = Completion(
            text=text,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cost_usd=usage.get("cost_usd"),
            raw={"exit_code": 0},
        )
        if completion.cost_usd is None:
            completion.cost_usd = estimate_cost(
                str(self.spec.params.get("model", self.spec.id)),
                completion.input_tokens,
                completion.output_tokens,
                self.spec.params,
            )
        return completion
