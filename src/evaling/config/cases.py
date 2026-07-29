"""Load and normalize test cases: inline lists and CSV/JSONL datasets.

Dataset rows are flat mappings. The keys ``id``, ``expected``, ``human_label``,
and ``files`` are case fields; every other key becomes a template variable. A
string value of the form ``file://<path>`` marks that field as a file
attachment instead of a variable.

Relative attachment paths resolve against the file that declares them: the
dataset file for dataset rows, the config directory for inline cases.
"""

import csv
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evaling.config.errors import ConfigError
from evaling.config.schema import Case, EvalConfig

RESERVED_FIELDS = frozenset({"id", "expected", "human_label", "files"})

FILE_PREFIX = "file://"


def load_cases(config: EvalConfig) -> list[Case]:
    """Produce the final case list: loaded, path-resolved, with unique ids."""
    if isinstance(config.cases, list):
        cases = [_resolve_files(case, config.base_dir) for case in config.cases]
    else:
        dataset = config.base_dir / config.cases.file
        cases = _load_dataset(dataset)
    return _assign_ids(cases)


def _load_dataset(path: Path) -> list[Case]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        rows = _read_jsonl(path)
    elif suffix == ".csv":
        rows = _read_csv(path)
    else:
        raise ConfigError(f"unsupported case file type {suffix!r}: {path} (use .jsonl or .csv)")
    if not rows:
        raise ConfigError(f"case file is empty: {path}")
    return [
        _resolve_files(_row_to_case(path, index, row), path.parent)
        for index, row in enumerate(rows, start=1)
    ]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise ConfigError(f"case file not found: {path}") from None
    rows = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path}:{number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise ConfigError(f"{path}:{number}: each line must be a JSON object")
        rows.append(row)
    return rows


def _read_csv(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                return []
            rows = list(reader)
    except FileNotFoundError:
        raise ConfigError(f"case file not found: {path}") from None
    # CSV cells are always strings; empty reserved fields mean "not provided".
    for row in rows:
        for field in ("id", "expected", "human_label"):
            if row.get(field) == "":
                row[field] = None
    return rows


def _row_to_case(path: Path, index: int, row: dict[str, Any]) -> Case:
    data: dict[str, Any] = {"vars": {}, "files": {}}
    for key, value in row.items():
        if key == "files":
            if not isinstance(value, dict):
                raise ConfigError(f"{path}: row {index}: 'files' must be a mapping")
            data["files"].update(value)
        elif key in RESERVED_FIELDS:
            # Before the file:// check: an `expected` value that happens to
            # start with file:// is a literal answer, not an attachment
            # named "expected".
            data[key] = value
        elif isinstance(value, str) and value.startswith(FILE_PREFIX):
            data["files"][key] = value[len(FILE_PREFIX) :]
        else:
            data["vars"][key] = value
    try:
        return Case.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ConfigError(f"{path}: row {index}: {first['msg']}") from exc


def _resolve_files(case: Case, base_dir: Path) -> Case:
    if not case.files:
        return case
    resolved = {
        name: str((path if (path := Path(raw)).is_absolute() else base_dir / path).resolve())
        for name, raw in case.files.items()
    }
    return case.model_copy(update={"files": resolved})


def _assign_ids(cases: list[Case]) -> list[Case]:
    final: list[Case] = []
    seen: set[str] = set()
    for index, case in enumerate(cases, start=1):
        case_id = case.id or f"case-{index}"
        if case_id in seen:
            raise ConfigError(f"duplicate case id: {case_id!r}")
        seen.add(case_id)
        final.append(case if case.id else case.model_copy(update={"id": case_id}))
    return final
