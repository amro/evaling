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
        snapshot = yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True)
        (path / "config.snapshot.yaml").write_text(snapshot)
        meta = {
            "id": run_id,
            "label": label,
            "status": "running",
            "started_at": _utcnow(),
            "finished_at": None,
            "config_sha256": hashlib.sha256(snapshot.encode()).hexdigest(),
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
        return RunWriter(path, meta)

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
        if not path.is_file():
            return []
        records = []
        for line in path.read_text().splitlines():
            if line.strip():
                records.append(ResultRecord(**json.loads(line)))
        return records


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
        if not self.results_path.is_file():
            return set()
        keys = set()
        for line in self.results_path.read_text().splitlines():
            if line.strip():
                data = json.loads(line)
                keys.add((data["variant"], data["model"], data["case_id"]))
        return keys

    def store_artifact(self, ref: MediaRef) -> str:
        """Copy a media file into artifacts/, content-addressed. Idempotent."""
        name = f"{ref.sha256}{ref.path.suffix.lower()}"
        dest = self.path / "artifacts" / name
        if not dest.exists():
            shutil.copyfile(ref.path, dest)
        return f"artifacts/{name}"

    def finalize(self, counts: dict[str, int], totals: dict[str, Any]) -> None:
        self.meta.update(
            status="complete",
            finished_at=_utcnow(),
            counts=counts,
            totals=totals,
        )
        _write_json(self.path / "run.json", self.meta)
