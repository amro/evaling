"""A JSONL trace of every provider call, for when a provider misbehaves.

The alternative is adding print statements to evaling and running it from a
checkout, which is a lot to ask of someone whose actual problem is that a
gateway is returning something odd. This writes the request body evaling sent
and the response it got back, per call, in a form you can grep and `jq`.

Off unless asked for. Two rules make it safe to turn on:

* **No headers, ever.** The API key lives in a header, so the simplest way to
  guarantee a log never contains one is never to write them. Bodies are
  additionally scrubbed of every credential evaling knows about — values from
  a secrets file *and* each model's resolved API key, which providers register
  via :meth:`RequestLog.add_secret` — for a gateway that reflects credentials
  into its own responses.
* **Refused under no-look.** A verbatim record of prompts and completions is
  the exact artifact that mode exists to prevent, so asking for both is a
  contradiction rather than a preference.

What it cannot promise: a ``command`` provider's script gets the environment,
and a script that prints some *other* credential to stderr will have that
logged. evaling knows the keys its own models declare, not the ones your
script reads.
"""

import json
from pathlib import Path
from typing import Any

from evaling.errors import EvalingError
from evaling.secrets import redact

TRACE_MARKER = "_evaling_request_log"
TRACE_VERSION = 1


def _refuse_to_clobber(path: Path) -> None:
    """Refuse a target that is not an empty file or a trace we wrote.

    Each run truncates its log, and `--log-requests eval.yaml` is an easy
    thing to type. Destroying the file someone pointed at is not a thing a
    debugging flag gets to do. Every line must carry our format marker:
    ordinary datasets and result files are JSONL too. Old, unmarked traces
    are deliberately refused rather than guessed at.
    """
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                entry = json.loads(line)
                if (
                    not isinstance(entry, dict)
                    or type(entry.get(TRACE_MARKER)) is not int
                    or entry[TRACE_MARKER] != TRACE_VERSION
                ):
                    raise ValueError("not an evaling request log")
    except (OSError, ValueError):
        raise EvalingError(
            f"refusing to overwrite {path}: its content could not be verified as "
            "an evaling request log. Each run truncates its log, so point "
            "--log-requests at a new file."
        ) from None


class RequestLog:
    """Append-only JSONL. One line per provider call."""

    #: Below this length a "secret" is too short to redact without mangling
    #: ordinary text.
    MIN_SECRET = 8

    def __init__(self, path: str | Path, secret_values: "list[str] | tuple[str, ...]" = ()):
        self.path = Path(path)
        self._secrets = [value for value in secret_values if value]
        _refuse_to_clobber(self.path)
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

    def add_secret(self, value: "str | None") -> None:
        """Register a credential to scrub from every entry, past this point.

        Providers call this with the key they resolved. A secrets file's
        values are known up front, but a key from the real environment — the
        normal case — is only known to the provider that looks it up, and
        without this a gateway reflecting the header into its error body would
        put that key on disk. The error-message path already defends against
        exactly that; this is the same defence for the log.
        """
        if value and len(value) >= self.MIN_SECRET and value not in self._secrets:
            self._secrets.append(value)

    def record(self, **fields: Any) -> None:
        """Write one entry. Never raises: logging must not fail a run."""
        try:
            line = json.dumps({**fields, TRACE_MARKER: TRACE_VERSION}, default=str, sort_keys=True)
        except (TypeError, ValueError):
            # `default=str` handles almost everything, but not a circular
            # structure: json.dumps raises before `default` is consulted.
            line = json.dumps({"error": "entry was not serializable", TRACE_MARKER: TRACE_VERSION})
        line = redact(line, self._secrets)
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        except OSError:
            # A full disk is not a reason to lose the run this was diagnosing.
            pass


def check_target(path: "str | Path | None", *, no_look: bool) -> None:
    """Refuse an impossible request log, without creating anything.

    Separate from :func:`open_log` so a caller can settle this before starting
    a run. The CLI does: a run that appears to begin and then fails on its own
    arguments reads as a crash rather than a refusal.
    """
    if path is None:
        return
    if not str(path).strip():
        raise EvalingError("request log was given an empty path")
    if no_look:
        raise EvalingError(
            "a request log cannot be written in no-look mode: it records prompts and "
            "completions verbatim, which is exactly what the mode exists to prevent. "
            "Reproduce the problem on data you are allowed to read instead."
        )
    # Here too, not only in RequestLog: the CLI calls this before it starts a
    # run, and a refusal that arrives after the progress bar reads as a crash.
    _refuse_to_clobber(Path(path))


def open_log(path: "str | Path | None", env: Any, *, no_look: bool) -> "RequestLog | None":
    """Build the log a run should use, or None. Refuses under no-look."""
    check_target(path, no_look=no_look)
    if path is None:
        return None
    return RequestLog(path, getattr(env, "secret_values", ()))
