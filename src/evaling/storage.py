"""Run storage: plain files under the output directory.

Each run is a directory:

    <output_dir>/<run-id>/
      run.json               # metadata + aggregates (rewritten at finalize)
      config.snapshot.yaml   # canonical dump of the config used
      results.jsonl          # one record per variant×model×case, appended as completed
      artifacts/             # content-addressed binary inputs

Stored files are the source of truth; exports and reports are views over them.
"""

import hashlib
import json
import os
import secrets
import shutil
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from evaling.config.schema import EvalConfig
from evaling.content import MediaRef
from evaling.errors import EvalingError
from evaling.render import RenderedMessage, RenderedText


class StorageError(EvalingError):
    """A run could not be stored or loaded."""


@dataclass
class ResultRecord:
    """One cell of the eval matrix: a variant × model × case outcome."""

    variant: str
    model: str
    case_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    output: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: float | None = None
    cached: bool = False
    error: str | None = None
    scores: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.variant, self.model, self.case_id)


def serialize_messages(
    messages: list[RenderedMessage], *, include_source: bool = True
) -> list[dict[str, Any]]:
    """JSON-serializable form of rendered messages.

    Media parts are content-addressed (kind, media type, sha256); the source
    path is included for storage but excluded for cache keys, so identical
    content caches identically wherever it lives.
    """
    serialized = []
    for message in messages:
        parts: list[dict[str, Any]] = []
        for part in message.parts:
            if isinstance(part, RenderedText):
                parts.append({"type": "text", "text": part.text})
            else:
                media: dict[str, Any] = {
                    "type": part.kind,
                    "media_type": part.media_type,
                    "sha256": part.sha256,
                }
                if include_source:
                    media["source"] = str(part.path)
                parts.append(media)
        serialized.append({"role": message.role, "parts": parts})
    return serialized


def snapshot_config(config: EvalConfig, *, redact_cases: bool = False) -> tuple[str, str]:
    """Canonical YAML serialization of a config and its sha256."""
    data = config.model_dump(mode="json")
    if redact_cases:
        from evaling.privacy import redact_config_snapshot

        data = redact_config_snapshot(data)
    snapshot = yaml.safe_dump(data, sort_keys=True)
    return snapshot, hashlib.sha256(snapshot.encode()).hexdigest()


def _iter_result_lines(path: Path) -> Iterator[dict[str, Any]]:
    """Parse results.jsonl one line at a time, tolerating a torn final line.

    A process killed mid-append leaves a truncated last line; that is the
    normal crash artifact resume exists for, so it reads as end-of-file.
    Corruption anywhere else is a real error. One line of lookahead decides
    which case applies, so the file is never held in memory whole — these
    files are exactly the ones that grow to millions of lines.
    """
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as handle:
        pending: str | None = None
        number = 0
        for line in handle:
            if not line.strip():
                continue
            if pending is not None:
                number += 1
                try:
                    yield json.loads(pending)
                except json.JSONDecodeError as exc:
                    raise StorageError(f"{path}: corrupt record at line {number}") from exc
            pending = line
        if pending is not None:
            try:
                data = json.loads(pending)
            except json.JSONDecodeError:
                return  # torn tail from a crash mid-write
            yield data


def _read_result_lines(path: Path) -> list[dict[str, Any]]:
    return list(_iter_result_lines(path))


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


#: Bumped when the on-disk layout changes incompatibly. Recorded in run.json
#: so a future evaling can migrate — or refuse — instead of crashing on an
#: unexpected field.
FORMAT_VERSION = 1


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write JSON via a temp file + rename, so readers never see a partial file.

    A crash mid-write used to leave corrupt JSON, and one corrupt run.json
    broke listing for every run.
    """
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
        tmp.replace(path)  # atomic within a filesystem
    finally:
        tmp.unlink(missing_ok=True)


def _known_fields(cls) -> set[str]:
    return {f.name for f in fields(cls)}


def record_from_dict(data: dict[str, Any]) -> ResultRecord:
    """Build a ResultRecord, ignoring fields a newer evaling may have added.

    Stored runs are meant to stay readable; an unknown key should not be a
    TypeError.
    """
    known = _known_fields(ResultRecord)
    return ResultRecord(**{k: v for k, v in data.items() if k in known})


#: Labels `resolve_ref` interprets as something other than a label. A run
#: carrying one would be shadowed by the meaning and unreachable by name.
RESERVED_LABELS = frozenset({"latest", "baseline"})


def check_label(label: str | None) -> None:
    """Refuse a label that could never be used to refer to the run.

    Called by `create_run`, and separately by the CLI before a run starts:
    the store is not created until the engine is already underway, so relying
    on `create_run` alone meant the refusal arrived after the run appeared to
    begin. Nothing was spent — but it looked like something had been.
    """
    if label in RESERVED_LABELS:
        raise StorageError(f"label {label!r} is reserved as a run reference; choose another label")


class RunStore:
    """Creates, lists, and loads runs under one output directory."""

    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)

    def create_run(
        self,
        config: EvalConfig,
        *,
        label: str | None = None,
        config_sha256: str | None = None,
        redact_cases: bool = False,
        selection: dict[str, Any] | None = None,
        matrix: dict[str, Any] | None = None,
    ) -> "RunWriter":
        """Create a run directory.

        ``config_sha256`` overrides the recorded config hash — the engine
        passes a content fingerprint covering referenced files; the default is
        the hash of the config snapshot alone.

        ``redact_cases`` drops inline cases from the stored snapshot. A config
        that lists its cases inline contains the data itself, which no-look
        mode must not leave on disk.
        """
        check_label(label)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(20):
            now = datetime.now(timezone.utc)
            # millisecond precision so ids sort by creation order; the suffix
            # only disambiguates true collisions
            stamp = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}"
            run_id = f"{stamp}-{secrets.token_hex(2)}"
            path = self.output_dir / run_id
            try:
                path.mkdir()
                break
            except FileExistsError:
                continue
        else:  # pragma: no cover - 20 collisions in a second is not a real scenario
            raise StorageError(f"could not allocate a unique run directory in {self.output_dir}")

        (path / "artifacts").mkdir()
        snapshot, snapshot_sha256 = snapshot_config(config, redact_cases=redact_cases)
        config_sha256 = config_sha256 or snapshot_sha256
        (path / "config.snapshot.yaml").write_text(snapshot, encoding="utf-8", newline="\n")
        meta = {
            "format_version": FORMAT_VERSION,
            "id": run_id,
            "label": label,
            "status": "running",
            "started_at": _utcnow(),
            # Authoritative creation order: id timestamps only have millisecond
            # precision, so two rapid runs' ids may sort by their random suffix.
            "created_ns": time.time_ns(),
            "finished_at": None,
            "config_sha256": config_sha256,
            # How the cases were narrowed, when they were. Resume reads this
            # back so the second half of a run draws the same sample as the
            # first.
            "selection": selection,
            # The shape the run set out to execute. Resume compares it, so a
            # run cannot be finished with a different set of filters than it
            # started with.
            "matrix": matrix,
            "counts": None,
            "totals": None,
            # Recorded so a reader can tell "the model returned nothing" from
            # "the answer was never stored" — the two look identical in a
            # record, since both leave the messages and output empty.
            "no_look": redact_cases,
        }
        write_json_atomic(path / "run.json", meta)
        return RunWriter(path, meta)

    def open_run(self, run_id: str, *, for_write: bool = True) -> "RunWriter":
        """Open a run. Only a write-open repairs a torn tail.

        Reads (show, compare, exports, MCP) must not mutate a run directory —
        two concurrent readers would otherwise race on that repair write.
        """
        path = self.output_dir / run_id
        meta_path = path / "run.json"
        if not meta_path.is_file():
            raise StorageError(f"run not found: {run_id!r} (no {meta_path})")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StorageError(f"{meta_path}: run metadata is corrupt") from exc
        writer = RunWriter(path, meta)
        if for_write:
            writer.repair_torn_tail()
        return writer

    def list_runs(self) -> list[dict[str, Any]]:
        """Metadata of all runs, oldest first (by creation time, id as tiebreak)."""
        if not self.output_dir.is_dir():
            return []
        runs = []
        for entry in self.output_dir.iterdir():
            meta_path = entry / "run.json"
            if not meta_path.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # A half-written run (killed mid-create) must not poison the
                # listing of every other run.
                continue
            # Valid JSON is not necessarily a run: `null`, a list, or a
            # mapping with no id all parse, and the sort below would then
            # raise for the whole listing rather than skip the one directory.
            if isinstance(meta, dict) and meta.get("id"):
                runs.append(meta)
        runs.sort(key=lambda meta: (meta.get("created_ns") or 0, meta["id"]))
        return runs

    def load_spend(self, run_id: str) -> dict[str, float]:
        """Spend recorded during a run, whether or not it reached finalize().

        Falls back to the run's totals, which is where this lived before
        spend.json existed and is all a run written by an older evaling has.
        """
        path = self.output_dir / run_id / "spend.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if isinstance(data, dict):
                return {k: float(v or 0.0) for k, v in data.items()}
        totals = self.load_meta(run_id).get("totals") or {}
        return {
            "judge_cost_usd": float(totals.get("judge_cost_usd") or 0.0),
            "unattributed_cost_usd": float(totals.get("unattributed_cost_usd") or 0.0),
        }

    def load_meta(self, run_id: str) -> dict[str, Any]:
        return self.open_run(run_id, for_write=False).meta

    def load_results(self, run_id: str) -> list[ResultRecord]:
        return list(self.iter_results(run_id))

    def iter_results(self, run_id: str) -> "Iterator[ResultRecord]":
        """Stream records without holding the whole run in memory.

        Prefer this for scanning or paginating a large run; load_results is
        the convenience wrapper for when you genuinely want the list.
        """
        path = self.output_dir / run_id / "results.jsonl"
        for data in _iter_result_lines(path):
            yield record_from_dict(data)

    @property
    def _baseline_path(self) -> Path:
        return self.output_dir / "baseline"

    def set_baseline(self, run_id: str) -> None:
        """Pin a run as the baseline for regression gating."""
        self.load_meta(run_id)  # verify it exists
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._baseline_path.write_text(run_id + "\n", encoding="utf-8", newline="\n")

    def get_baseline(self) -> str | None:
        if not self._baseline_path.is_file():
            return None
        return self._baseline_path.read_text(encoding="utf-8").strip() or None

    def resolve_ref(self, ref: str) -> str:
        """Resolve a run reference to a run id.

        Accepts: a run id, ``latest``, ``baseline`` (the pinned run), or a
        run label (most recent match wins).
        """
        if ref == "latest":
            runs = self.list_runs()
            if not runs:
                raise StorageError("no runs found")
            return runs[-1]["id"]
        if ref == "baseline":
            baseline = self.get_baseline()
            if baseline is None:
                raise StorageError(
                    "no baseline pinned (use `evaling baseline set <run>` to pin one)"
                )
            return baseline
        if (self.output_dir / ref / "run.json").is_file():
            return ref
        labeled = [run for run in self.list_runs() if run.get("label") == ref]
        if labeled:
            return labeled[-1]["id"]
        raise StorageError(f"no run matches {ref!r} (not an id, label, 'latest', or 'baseline')")


class RunWriter:
    """Appends results and artifacts to one run directory."""

    def __init__(self, path: Path, meta: dict[str, Any]):
        self.path = path
        self.meta = meta

    @property
    def run_id(self) -> str:
        return self.meta["id"]

    @property
    def results_path(self) -> Path:
        return self.path / "results.jsonl"

    def append_result(self, record: ResultRecord) -> None:
        with self.results_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")

    @property
    def spend_path(self) -> Path:
        return self.path / "spend.json"

    def record_spend(self, **amounts: float) -> None:
        """Persist spend that no result record carries.

        Judge calls are not cells, and a cell refused a judge by the cost
        ceiling is dropped on purpose — so neither leaves anything in
        results.jsonl. Until this existed both lived only in memory until
        `finalize()`, which meant a killed run forgot them and a resume both
        under-reported the total and re-spent the ceiling.
        """
        write_json_atomic(self.spend_path, {k: round(v, 10) for k, v in amounts.items()})

    def completed_keys(self) -> set[tuple[str, str, str]]:
        """Keys of already-recorded results (for resume)."""
        return {
            (data["variant"], data["model"], data["case_id"])
            for data in _read_result_lines(self.results_path)
        }

    def repair_torn_tail(self) -> None:
        """Drop a truncated final line left by a process killed mid-append.

        Called when opening a run for resume, so subsequent appends never land
        after a torn line (which would corrupt the middle of the file).
        """
        if not self.results_path.is_file():
            return
        raw_lines = [
            line
            for line in self.results_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        good = _read_result_lines(self.results_path)
        if len(good) < len(raw_lines):
            content = "".join(json.dumps(data, sort_keys=True) + "\n" for data in good)
            self.results_path.write_text(content, encoding="utf-8", newline="\n")

    def store_artifact(self, ref: MediaRef) -> str:
        """Copy a media file into artifacts/, content-addressed. Idempotent."""
        name = f"{ref.sha256}{ref.path.suffix.lower()}"
        dest = self.path / "artifacts" / name
        if not dest.exists():
            # temp + rename, like every other write here: a crash mid-copy
            # must not leave a partial file that the existence check then
            # trusts forever. The random suffix keeps concurrent cells (same
            # pid, different threads) from interleaving on one temp file.
            tmp = dest.with_name(f".{name}.tmp{os.getpid()}.{secrets.token_hex(4)}")
            try:
                shutil.copyfile(ref.path, tmp)
                tmp.replace(dest)
            finally:
                tmp.unlink(missing_ok=True)
        return f"artifacts/{name}"

    def finalize(
        self,
        counts: dict[str, int],
        totals: dict[str, Any],
        aggregates: dict[str, Any] | None = None,
        gate: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        stopped_early: bool = False,
        status: str = "complete",
    ) -> None:
        self.meta.update(
            status=status,
            # Complete, but deliberately short: --fail-fast ended it at the
            # first failing cell. Distinct from a run that was interrupted,
            # which never reaches finalize at all.
            stopped_early=stopped_early,
            finished_at=_utcnow(),
            counts=counts,
            totals=totals,
            aggregates=aggregates,
            gate=gate,
            warnings=warnings or [],
        )
        write_json_atomic(self.path / "run.json", self.meta)
