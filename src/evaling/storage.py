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
import secrets
import shutil
from dataclasses import asdict, dataclass, field
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


def snapshot_config(config: EvalConfig) -> tuple[str, str]:
    """Canonical YAML serialization of a config and its sha256."""
    snapshot = yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True)
    return snapshot, hashlib.sha256(snapshot.encode()).hexdigest()


def _read_result_lines(path: Path) -> list[dict[str, Any]]:
    """Parse results.jsonl, tolerating a torn final line.

    A process killed mid-append leaves a truncated last line; that is the
    normal crash artifact resume exists for, so it reads as end-of-file.
    Corruption anywhere else is a real error.
    """
    if not path.is_file():
        return []
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    records = []
    for index, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            if index == len(lines) - 1:
                break  # torn tail from a crash mid-write
            raise StorageError(f"{path}: corrupt record at line {index + 1}") from exc
    return records


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


class RunStore:
    """Creates, lists, and loads runs under one output directory."""

    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)

    def create_run(self, config: EvalConfig, *, label: str | None = None) -> "RunWriter":
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(20):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
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
        snapshot, config_sha256 = snapshot_config(config)
        (path / "config.snapshot.yaml").write_text(snapshot)
        meta = {
            "id": run_id,
            "label": label,
            "status": "running",
            "started_at": _utcnow(),
            "finished_at": None,
            "config_sha256": config_sha256,
            "counts": None,
            "totals": None,
        }
        _write_json(path / "run.json", meta)
        return RunWriter(path, meta)

    def open_run(self, run_id: str) -> "RunWriter":
        """Open an existing run for resuming."""
        path = self.output_dir / run_id
        meta_path = path / "run.json"
        if not meta_path.is_file():
            raise StorageError(f"run not found: {run_id!r} (no {meta_path})")
        meta = json.loads(meta_path.read_text())
        writer = RunWriter(path, meta)
        writer.repair_torn_tail()
        return writer

    def list_runs(self) -> list[dict[str, Any]]:
        """Metadata of all runs, oldest first (ids are timestamp-sortable)."""
        if not self.output_dir.is_dir():
            return []
        runs = []
        for entry in sorted(self.output_dir.iterdir()):
            meta_path = entry / "run.json"
            if meta_path.is_file():
                runs.append(json.loads(meta_path.read_text()))
        return runs

    def load_meta(self, run_id: str) -> dict[str, Any]:
        return self.open_run(run_id).meta

    def load_results(self, run_id: str) -> list[ResultRecord]:
        path = self.output_dir / run_id / "results.jsonl"
        return [ResultRecord(**data) for data in _read_result_lines(path)]

    @property
    def _baseline_path(self) -> Path:
        return self.output_dir / "baseline"

    def set_baseline(self, run_id: str) -> None:
        """Pin a run as the baseline for regression gating."""
        self.load_meta(run_id)  # verify it exists
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._baseline_path.write_text(run_id + "\n")

    def get_baseline(self) -> str | None:
        if not self._baseline_path.is_file():
            return None
        return self._baseline_path.read_text().strip() or None

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
        with self.results_path.open("a") as handle:
            handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")

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
        raw_lines = [line for line in self.results_path.read_text().splitlines() if line.strip()]
        good = _read_result_lines(self.results_path)
        if len(good) < len(raw_lines):
            content = "".join(json.dumps(data, sort_keys=True) + "\n" for data in good)
            self.results_path.write_text(content)

    def store_artifact(self, ref: MediaRef) -> str:
        """Copy a media file into artifacts/, content-addressed. Idempotent."""
        name = f"{ref.sha256}{ref.path.suffix.lower()}"
        dest = self.path / "artifacts" / name
        if not dest.exists():
            shutil.copyfile(ref.path, dest)
        return f"artifacts/{name}"

    def finalize(
        self,
        counts: dict[str, int],
        totals: dict[str, Any],
        aggregates: dict[str, Any] | None = None,
        gate: dict[str, Any] | None = None,
    ) -> None:
        self.meta.update(
            status="complete",
            finished_at=_utcnow(),
            counts=counts,
            totals=totals,
            aggregates=aggregates,
            gate=gate,
        )
        _write_json(self.path / "run.json", self.meta)
