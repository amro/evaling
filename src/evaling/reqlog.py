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
import os
import stat
from contextlib import suppress
from pathlib import Path
from typing import Any, TextIO

from evaling.errors import EvalingError
from evaling.secrets import redact

TRACE_MARKER = "_evaling_request_log"
TRACE_VERSION = 1


def _validate_trace(handle: TextIO) -> None:
    """Validate every line of the opened file, before truncating that same file."""
    for line in handle:
        entry = json.loads(line)
        if (
            not isinstance(entry, dict)
            or type(entry.get(TRACE_MARKER)) is not int
            or entry[TRACE_MARKER] != TRACE_VERSION
        ):
            raise ValueError("not an evaling request log")
    handle.seek(0)


def _open_checked(path: Path, *, create: bool) -> TextIO | None:
    """Open without truncation, refuse links/devices, and validate the descriptor.

    O_NONBLOCK keeps a concurrently substituted FIFO from blocking on POSIX.
    O_NOFOLLOW rejects final-component symlinks there; lstat/fstat identity
    checks also protect platforms without that flag. Parent directories and
    concurrent writes to the same inode must still be trusted.
    """
    descriptor = None
    try:
        try:
            before = path.lstat()
        except FileNotFoundError:
            if not create:
                return None
            before = None
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise ValueError("target must be a regular file, not a link or device")
        flags = os.O_RDWR if create else os.O_RDONLY
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_BINARY", 0)
        if before is None:
            flags |= os.O_CREAT | os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or not os.path.samestat(opened, current)
            or (before is not None and not os.path.samestat(before, opened))
            or opened.st_nlink != 1
        ):
            raise ValueError("target changed or is not a single regular file")
        handle = os.fdopen(descriptor, "r+" if create else "r", encoding="utf-8", newline="\n")
        descriptor = None  # the file object now owns it
        try:
            _validate_trace(handle)
        except BaseException:
            handle.close()
            raise
        return handle
    except (OSError, ValueError) as exc:
        raise EvalingError(
            f"refusing to overwrite {path}: could not open request log or verify "
            f"a regular evaling trace ({exc}). Each run truncates its log, so point "
            "--log-requests at a new file."
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _refuse_to_clobber(path: Path) -> None:
    handle = _open_checked(path, create=False)
    if handle is not None:
        handle.close()


class RequestLog:
    """Append-only JSONL. One line per provider call."""

    #: Below this length a "secret" is too short to redact without mangling
    #: ordinary text.
    MIN_SECRET = 8

    def __init__(self, path: str | Path, secret_values: "list[str] | tuple[str, ...]" = ()):
        self.path = Path(path)
        self._secrets = [value for value in secret_values if value]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = _open_checked(self.path, create=True)
            try:
                self._handle.truncate(0)
            except BaseException:
                self._handle.close()
                raise
        except (OSError, ValueError) as exc:
            # ValueError as well as OSError: a path with a NUL in it, which an
            # unset shell variable can produce, raises from io.open rather than
            # from the filesystem.
            raise EvalingError(f"could not open request log {self.path}: {exc}") from exc

    def close(self) -> None:
        """Release the verified file; safe to call more than once."""
        with suppress(OSError):
            self._handle.close()

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
            self._handle.write(line + "\n")
            self._handle.flush()
        except (OSError, ValueError):
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
