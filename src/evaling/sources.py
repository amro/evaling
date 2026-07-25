"""Case sources: cases fetched from somewhere, a page at a time.

A config can list cases inline or point at a dataset file, but neither works
when the cases live behind an API and there are hundreds of thousands of them.
A source is an object you write that evaling calls for pages of cases.

Implement :class:`CaseSource` — structurally, no import or subclass required::

    class ProdTraffic:
        def fetch(self, cursor, limit):
            page = my_api.query(after=cursor, limit=limit)
            return CasePage(
                cases=[Case(id=row["id"], vars={"question": row["q"]}) for row in page.rows],
                cursor=page.next_cursor,   # None when there is no more
            )

``fetch`` may be sync or async. Two optional methods are used when present:
``count()`` for progress and cost confirmation, and ``close()`` for cleanup.

Cursor-based rather than offset-based, so that rows inserted or deleted during
a walk do not cause pages to skip or repeat.
"""

import importlib.util
import inspect
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from evaling.config.errors import ConfigError
from evaling.config.schema import Case

#: Cases requested per fetch when the config doesn't say.
DEFAULT_PAGE_SIZE = 100


@dataclass(frozen=True)
class CasePage:
    """One page of cases, plus where to continue from."""

    cases: Sequence[Case] = field(default_factory=tuple)
    #: Opaque marker for the next fetch; None means this was the last page.
    cursor: str | None = None


@runtime_checkable
class CaseSource(Protocol):
    """What evaling needs from a source. Structural: implement, don't inherit."""

    def fetch(self, cursor: str | None, limit: int) -> CasePage: ...


class BaseCaseSource(ABC):
    """Optional base class, for discoverability and editor completion.

    Implementing :class:`CaseSource` directly works just as well.
    """

    @abstractmethod
    def fetch(self, cursor: str | None, limit: int) -> CasePage:
        """Return up to ``limit`` cases starting after ``cursor``."""

    def count(self) -> int | None:
        """Total cases, if knowable cheaply. None disables progress totals."""
        return None

    def close(self) -> None:
        """Release anything the source holds open. Nothing to do by default."""
        return None


class SourceError(ConfigError):
    """A case source could not be loaded or produced something unusable."""


def load_source(spec: str, base_dir: Path, params: dict[str, Any] | None = None) -> Any:
    """Build a source from a ``path/to/file.py:factory`` reference.

    The factory is called with ``**params`` and must return an object with a
    ``fetch`` method. Mirrors how the ``python`` scorer loads user code, so
    there is one convention for "point evaling at some of my Python".
    """
    if ":" not in spec:
        raise SourceError(
            f"case source {spec!r} must be 'path/to/file.py:factory' "
            "(the callable that builds your source)"
        )
    file_part, _, attribute = spec.rpartition(":")
    path = (base_dir / file_part).resolve()
    if not path.is_file():
        raise SourceError(f"case source: file not found: {path}")

    module_spec = importlib.util.spec_from_file_location(f"evaling_source_{path.stem}", path)
    module = importlib.util.module_from_spec(module_spec)
    try:
        module_spec.loader.exec_module(module)
    except Exception as exc:
        raise SourceError(f"case source: error importing {path}: {exc}") from exc

    factory = getattr(module, attribute, None)
    if factory is None:
        raise SourceError(f"case source: no {attribute!r} in {path}")
    try:
        source = factory(**(params or {}))
    except TypeError as exc:
        raise SourceError(f"case source: {attribute}(...) rejected its params: {exc}") from exc
    except Exception as exc:
        raise SourceError(
            f"case source: {attribute}(...) raised {type(exc).__name__}: {exc}"
        ) from exc

    if not hasattr(source, "fetch"):
        raise SourceError(
            f"case source: {attribute}(...) returned {type(source).__name__}, "
            "which has no fetch(cursor, limit) method"
        )
    return source


async def _call_fetch(source: Any, cursor: str | None, limit: int) -> CasePage:
    result = source.fetch(cursor, limit)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, CasePage):
        raise SourceError(
            f"case source fetch() returned {type(result).__name__}, expected a CasePage"
        )
    return result


async def source_count(source: Any) -> int | None:
    """The source's own total, when it offers one."""
    counter = getattr(source, "count", None)
    if counter is None:
        return None
    try:
        value = counter()
        if inspect.isawaitable(value):
            value = await value
    except Exception as exc:
        raise SourceError(f"case source count() raised {type(exc).__name__}: {exc}") from exc
    if value is None:
        return None
    if not isinstance(value, int) or value < 0:
        raise SourceError(f"case source count() returned {value!r}, expected a non-negative int")
    return value


async def close_source(source: Any) -> None:
    closer = getattr(source, "close", None)
    if closer is None:
        return
    result = closer()
    if inspect.isawaitable(result):
        await result


async def iter_source_cases(
    source: Any, page_size: int = DEFAULT_PAGE_SIZE, limit: int | None = None
) -> AsyncIterator[Case]:
    """Yield cases page by page, never holding more than one page.

    Stops at ``limit`` cases if given, at the source's last page otherwise. A
    source that keeps returning the same cursor would spin forever, so a cursor
    that does not advance ends iteration.
    """
    if page_size < 1:
        raise SourceError("page_size must be >= 1")
    cursor: str | None = None
    seen = 0
    seen_cursors: set[str] = set()
    while True:
        want = page_size if limit is None else min(page_size, limit - seen)
        if want <= 0:
            return
        page = await _call_fetch(source, cursor, want)
        if not page.cases:
            return
        for case in page.cases:
            if not isinstance(case, Case):
                raise SourceError(
                    f"case source yielded {type(case).__name__}, expected evaling.Case"
                )
            yield case
            seen += 1
            if limit is not None and seen >= limit:
                return
        if page.cursor is None:
            return
        if page.cursor in seen_cursors:
            raise SourceError(
                f"case source returned cursor {page.cursor!r} twice; "
                "a cursor must advance or be None on the last page"
            )
        seen_cursors.add(page.cursor)
        cursor = page.cursor
