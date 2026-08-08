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

import asyncio
import importlib.util
import inspect
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from evaling.config.cases import _resolve_files
from evaling.config.errors import ConfigError
from evaling.config.schema import Case

#: Cases requested per fetch when the config doesn't say.
DEFAULT_PAGE_SIZE = 100

#: Consecutive empty pages tolerated before a source is called broken. A source
#: filtering its own rows legitimately returns some; it does not return
#: thousands while still claiming there is more to come.
MAX_EMPTY_PAGES = 1000


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


async def _call_user(fn: Any, *args: Any) -> Any:
    """Call a user-supplied source method without blocking the event loop.

    A sync ``fetch``/``count``/``close`` that does real I/O — a database
    query, an HTTP page — would otherwise stall every in-flight model call
    for its duration, the same reason the engine pushes cache and storage
    I/O through to_thread.
    """
    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    result = await asyncio.to_thread(fn, *args)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _call_fetch(source: Any, cursor: str | None, limit: int) -> CasePage:
    result = await _call_user(source.fetch, cursor, limit)
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
        value = await _call_user(counter)
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
    await _call_user(closer)


async def iter_source_cases(
    source: Any,
    page_size: int = DEFAULT_PAGE_SIZE,
    limit: int | None = None,
    base_dir: Path | None = None,
) -> AsyncIterator[Case]:
    """Yield cases page by page, never holding more than one page.

    Given ``base_dir``, each case's attachments are contained under it, the
    same as a dataset's. A source is the config author's own Python, but the
    rows it returns usually are not — they come from an API, a warehouse, a
    vendor export — so a `files` value is untrusted data arriving through
    trusted code, which is exactly what containment is for. Passing None skips
    the check and is only correct where no attachment will be read.

    Stops at ``limit`` cases if given, at the source's last page otherwise. A
    source that keeps returning the same cursor would spin forever, so a cursor
    that does not advance is an error.

    A case without an ``id`` is numbered by position, as inline and dataset
    cases are. Uniqueness is *not* checked: that needs every id at once, which
    is the one thing streaming exists to avoid, so a source handing out
    duplicate ids produces records that share one.

    Every cursor seen is remembered, so a cursor that repeats at any distance
    is caught rather than only one that repeats immediately: an A→B→A cycle
    advances at every step and never ends, and comparing against the previous
    cursor alone walks it forever — yielding the same cases over and over, or
    silently filling a ``limit`` with duplicates. That costs one short string
    per page walked, which is the one thing here that is not constant, and is
    worth it to bound the loop.
    """
    if page_size < 1:
        raise SourceError("page_size must be >= 1")
    cursor: str | None = None
    seen = 0
    empty_pages = 0
    seen_cursors: set[str] = set()
    while True:
        want = page_size if limit is None else min(page_size, limit - seen)
        if want <= 0:
            return
        page = await _call_fetch(source, cursor, want)
        # An empty page is only the end when the cursor says so. Treating any
        # empty page as the end truncated the run silently: a source that
        # filters its own rows — which docs/large-datasets.md recommends —
        # returns an empty page with a live cursor whenever a whole page is
        # filtered out, and everything after it was dropped without a word.
        if not page.cases and page.cursor is None:
            return
        # Following those pages means a source can now advance forever without
        # yielding anything, which looks like a hang rather than a fault: no
        # cases, no cost, no progress. Bounded generously — a source filtering
        # out this many consecutive pages is broken, not thorough.
        empty_pages = empty_pages + 1 if not page.cases else 0
        if empty_pages > MAX_EMPTY_PAGES:
            raise SourceError(
                f"case source returned {empty_pages} pages in a row with no cases and "
                "more to come; it is filtering everything out or its cursor is stuck"
            )
        for case in page.cases:
            if not isinstance(case, Case):
                raise SourceError(
                    f"case source yielded {type(case).__name__}, expected evaling.Case"
                )
            if base_dir is not None:
                case = _resolve_files(case, base_dir, may_reach_outside=False)
            if not case.id:
                # Inline and dataset cases are numbered by _assign_ids; source
                # cases reach the engine directly and skipped it, so every
                # record carried an empty case id — which grouped unrelated
                # cases under one heading in the report and made them
                # indistinguishable in an export.
                case = case.model_copy(update={"id": f"case-{seen + 1}"})
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
