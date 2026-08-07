"""Load and normalize test cases: inline lists and CSV/JSONL datasets.

Dataset rows are flat mappings. The keys ``id``, ``expected``, ``human_label``,
and ``files`` are case fields; every other key becomes a template variable. A
string value of the form ``file://<path>`` marks that field as a file
attachment instead of a variable.

Relative attachment paths resolve against the file that declares them: the
dataset file for dataset rows, the config directory for inline cases.
"""

import csv
import io
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evaling.config.errors import ConfigError
from evaling.config.schema import Case, EvalConfig
from evaling.textfile import read_text

RESERVED_FIELDS = frozenset({"id", "expected", "human_label", "files"})

FILE_PREFIX = "file://"


def load_cases(config: EvalConfig) -> list[Case]:
    """Produce the final case list: loaded, path-resolved, with unique ids."""
    if isinstance(config.cases, list):
        # Inline cases live in the config, so an absolute path there is the
        # config author reaching outside on purpose.
        cases = [
            _resolve_files(case, config.base_dir, may_reach_outside=True) for case in config.cases
        ]
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
        _resolve_files(_row_to_case(path, index, row), path.parent, may_reach_outside=False)
        for index, row in enumerate(rows, start=1)
    ]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    lines = read_text(path, ConfigError, missing=f"case file not found: {path}").splitlines()
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
    raw = read_text(path, ConfigError, missing=f"case file not found: {path}")
    # StringIO rather than the file handle, so decoding failures are reported
    # by read_text alongside every other file's. Note that read_text has
    # already applied universal newlines, so a quoted field containing CRLF
    # arrives here with LF — the row survives, the line ending is normalized.
    try:
        reader = csv.DictReader(io.StringIO(raw, newline=""))
        if reader.fieldnames is None:
            return []
        rows = list(reader)
    except csv.Error as exc:
        # A NUL byte, or a field past the module's size limit. Neither is a
        # crash worth showing anyone the C stack for.
        raise ConfigError(f"{path}: could not parse as CSV: {exc}") from exc
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


def _resolve_files(case: Case, base_dir: Path, *, may_reach_outside: bool) -> Case:
    if not case.files:
        return case
    resolved = {
        name: _resolve_attachment(raw, base_dir, may_reach_outside=may_reach_outside)
        for name, raw in case.files.items()
    }
    return case.model_copy(update={"files": resolved})


def _resolve_attachment(raw: str, base_dir: Path, *, may_reach_outside: bool) -> str:
    """One attachment path, resolved and kept where it belongs.

    evaling reads every attachment, hashes it, sends it to a model API, and
    archives it in the run directory. Who wrote the path decides whether it may
    point outside the project.

    A relative path always stays under the file that declared it, whoever wrote
    it. An absolute one is an explicit reach outside, and only the config may
    make it: an inline case is written by whoever wrote the config, while a
    dataset arrives from elsewhere — a vendor, a colleague, an export.

    The containment check used to apply to relative paths alone, so a dataset
    escaped it by writing the same path absolute — `/home/you/.ssh/id_rsa`
    instead of `../../../.ssh/id_rsa` — which read, transmitted and archived
    the file exactly as the check existed to prevent.
    """
    path = Path(raw)
    root = base_dir.resolve()
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    if path.is_absolute() and may_reach_outside:
        return str(resolved)
    if resolved != root and root not in resolved.parents:
        # Two ways to land here, and the way out differs. A config's own
        # relative path can be made absolute; a dataset's cannot.
        way_out = (
            "Move the file under that directory, or use an absolute path if reaching "
            "outside is intended."
            if may_reach_outside
            else "Move the file under that directory, or declare it on an inline case "
            "in the config if reaching outside is intended."
        )
        raise ConfigError(
            f"attachment {raw!r} resolves outside {root} — it would be read, sent to a "
            f"model API, and archived with the run. {way_out}"
        )
    return str(resolved)


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
