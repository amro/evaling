"""Reading user-authored files, so that every way one can be wrong is a message.

Config, prompt, case, and secrets files are all typed by hand and all read the
same way: open as UTF-8, parse, complain usefully. Doing that inline in five
places meant five slightly different sets of caught exceptions, and the ones
nobody thought about escaped as tracebacks — a config saved in a legacy
encoding died inside the codec, and a deeply nested one died inside PyYAML.
Neither told the reader anything they could act on.

The rule for everything here: raise the caller's own error type with a message
naming the file, or return the parsed value. Nothing else gets out.
"""

from pathlib import Path
from typing import Any

import yaml

from evaling.errors import EvalingError


def read_text(path: Path, error: type[EvalingError], *, missing: str) -> str:
    """A file's text as UTF-8, or ``error``.

    ``missing`` is the message for a file that isn't there, because "config
    file not found" and "case file not found" are worth saying differently.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise error(missing) from None
    except UnicodeDecodeError as exc:
        # Not an OSError — it's a ValueError — so the obvious `except OSError`
        # around a read misses it entirely. Reached by a file saved as UTF-16
        # (some editors' default) or as latin-1 with an accent in it.
        raise error(
            f"{path}: not valid UTF-8 — byte {exc.object[exc.start]:#04x} at offset "
            f"{exc.start} is not. evaling reads text files as UTF-8; re-save this "
            "one in that encoding."
        ) from exc
    except OSError as exc:
        raise error(f"could not read {path}: {exc}") from exc


def read_yaml(path: Path, error: type[EvalingError], *, missing: str) -> Any:
    """Parsed YAML from a file, or ``error``. Never a traceback."""
    raw = read_text(path, error, missing=missing)
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise error(f"{path}: invalid YAML: {exc}") from exc
    except RecursionError as exc:
        # PyYAML composes recursively, so nesting deeper than the interpreter's
        # stack raises here. RecursionError is not a YAMLError, so it used to
        # come out as a wall of repeated frames.
        raise error(
            f"{path}: YAML is nested too deeply to parse. This is usually a "
            "runaway bracket or dash rather than a structure anyone meant."
        ) from exc
