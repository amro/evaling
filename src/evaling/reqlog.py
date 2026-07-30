"""A JSONL trace of every provider call, for when a provider misbehaves.

The alternative is adding print statements to evaling and running it from a
checkout, which is a lot to ask of someone whose actual problem is that a
gateway is returning something odd. This writes the request body evaling sent
and the response it got back, per call, in a form you can grep and `jq`.

Off unless asked for. Two rules make it safe to turn on:

* **No headers, ever.** The API key lives in a header, so the simplest way to
  guarantee a log never contains one is never to write them. Every value from
  a secrets file is additionally redacted from the bodies, for a gateway that
  reflects credentials into its own responses.
* **Refused under no-look.** A verbatim record of prompts and completions is
  the exact artifact that mode exists to prevent, so asking for both is a
  contradiction rather than a preference.
"""

import json
from pathlib import Path
from typing import Any

from evaling.errors import EvalingError
from evaling.secrets import redact


class RequestLog:
    """Append-only JSONL. One line per provider call."""

    def __init__(self, path: str | Path, secret_values: "list[str] | tuple[str, ...]" = ()):
        self.path = Path(path)
        self._secrets = tuple(secret_values)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Truncate: a log that accumulates across runs is unreadable, and
            # the interesting question is always about the run you just made.
            self.path.write_text("", encoding="utf-8", newline="\n")
        except (OSError, ValueError) as exc:
            # ValueError as well as OSError: a path with a NUL in it, which an
            # unset shell variable can produce, raises from io.open rather than
            # from the filesystem.
            raise EvalingError(f"could not open request log {self.path}: {exc}") from exc

    def record(self, **fields: Any) -> None:
        """Write one entry. Never raises: logging must not fail a run."""
        try:
            line = json.dumps(fields, default=str, sort_keys=True)
        except (TypeError, ValueError):  # pragma: no cover - default=str covers ~everything
            line = json.dumps({"error": "entry was not serializable"})
        line = redact(line, self._secrets)
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        except OSError:
            # A full disk is not a reason to lose the run this was diagnosing.
            pass


def open_log(path: str | Path | None, env: Any, *, no_look: bool) -> "RequestLog | None":
    """Build the log a run should use, or None. Refuses under no-look."""
    if path is None:
        return None
    if not str(path).strip():
        raise EvalingError("request log was given an empty path")
    if no_look:
        raise EvalingError(
            "a request log cannot be written in no-look mode: it records prompts and "
            "completions verbatim, which is exactly what the mode exists to prevent. "
            "Reproduce the problem on data you are allowed to read instead."
        )
    return RequestLog(path, getattr(env, "secret_values", ()))
