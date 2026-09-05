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
import os
import signal
import subprocess
import time
from typing import Any

from evaling.providers.base import Completion, CompletionRequest, Provider, ProviderError
from evaling.providers.pricing import estimate_cost
from evaling.secrets import redact
from evaling.storage import serialize_messages

DEFAULT_TIMEOUT_S = 300.0
_CLEANUP_TIMEOUT_S = 5.0


def _kill_windows_tree(pid: int) -> None:
    """taskkill /T includes descendants; Process.kill() only kills cmd.exe."""
    taskkill = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe")
    subprocess.run(
        [taskkill, "/PID", str(pid), "/T", "/F"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=_CLEANUP_TIMEOUT_S,
        check=False,
    )


async def _stop_command(process, communication) -> None:
    """Stop the owned command tree, finish pipe I/O, and reap the shell."""
    try:
        if os.name == "posix":
            # Each command has its own session/group. The group may still
            # exist after the shell exits, with descendants holding the pipes.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        elif os.name == "nt" and process.returncode is None:
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                await asyncio.to_thread(_kill_windows_tree, process.pid)
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        # Keep the original communicate task alive through timeout/cancel so
        # it closes stdin and drains stdout/stderr, rather than abandoning the
        # pipe transports when wait() returns for an already-exited shell.
        try:
            await asyncio.wait_for(communication, _CLEANUP_TIMEOUT_S)
        except (Exception, asyncio.CancelledError):
            pass  # cleanup must preserve the original timeout/cancellation
        finally:
            # No public Process.close() exists. Closing its transport is the
            # bounded fallback for a detached descendant retaining a pipe;
            # it also ensures finalizers never touch a closed event loop.
            process._transport.close()
            await process.wait()


async def _cleanup_command(process, communication) -> None:
    await _finish_cleanup(_stop_command(process, communication))


async def _cleanup_spawn(spawning) -> None:
    try:
        process = await spawning
    except Exception:
        return  # spawning failed; preserve the caller's cancellation
    await _stop_command(process, asyncio.create_task(process.communicate(b"")))


async def _finish_cleanup(awaitable) -> None:
    """Repeated cancellation must not interrupt the cleanup already in flight."""
    cleanup = asyncio.create_task(awaitable)
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError


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
        encoded = payload.encode()
        spawning = asyncio.create_task(
            asyncio.create_subprocess_shell(
                self.spec.command or "",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(self.env) if self.env is not None else None,
                # Run where the config lives, so `command: python3 score.py` means
                # the same thing as every other path in that config — and a config
                # is not silently dependent on the caller's working directory.
                cwd=str(self.base_dir) if self.base_dir else None,
                start_new_session=(os.name == "posix"),
            )
        )
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            # Keep ownership even if cancellation lands while asyncio is
            # handing the newly created process back to us.
            await _finish_cleanup(_cleanup_spawn(spawning))
            raise
        communication = asyncio.create_task(process.communicate(encoded))
        try:
            out, err = await asyncio.wait_for(asyncio.shield(communication), timeout=timeout)
        except asyncio.TimeoutError as exc:
            await _cleanup_command(process, communication)
            raise ProviderError(
                f"model {self.spec.id!r}: command timed out after {timeout}s", retryable=True
            ) from exc
        except BaseException:
            # Cancellation and unexpected pipe failures both own a live
            # process until cleanup completes.
            await _cleanup_command(process, communication)
            raise
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
