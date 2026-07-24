"""Render a stored run into machine- and human-readable formats.

Stored files are the source of truth; these are pure views over
(run.json metadata, results records).
"""

import csv
import io
import json
from dataclasses import asdict
from typing import Any

from evaling.errors import EvalingError
from evaling.scoring import cell_summary
from evaling.storage import ResultRecord

FORMATS = ("json", "csv", "md")


def export_run(meta: dict[str, Any], records: list[ResultRecord], fmt: str) -> str:
    if fmt == "json":
        return export_json(meta, records)
    if fmt == "csv":
        return export_csv(records)
    if fmt == "md":
        return export_md(meta, records)
    raise EvalingError(f"unknown export format {fmt!r} (choose from: {', '.join(FORMATS)})")


def export_json(meta: dict[str, Any], records: list[ResultRecord]) -> str:
    return json.dumps(
        {"run": meta, "results": [asdict(record) for record in records]},
        indent=2,
        sort_keys=True,
    )


def export_csv(records: list[ResultRecord]) -> str:
    criteria = sorted({name for record in records for name in record.scores})
    fields = [
        "variant",
        "model",
        "case_id",
        "cell_score",
        "cell_passed",
        *[f"score:{name}" for name in criteria],
        *[f"passed:{name}" for name in criteria],
        "cached",
        "latency_ms",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "error",
        "output",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for record in records:
        score, passed = cell_summary(record)
        row: dict[str, Any] = {
            "variant": record.variant,
            "model": record.model,
            "case_id": record.case_id,
            "cell_score": score,
            "cell_passed": passed,
            "cached": record.cached,
            "latency_ms": record.latency_ms,
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "cost_usd": record.cost_usd,
            "error": record.error,
            "output": record.output,
        }
        for name in criteria:
            entry = record.scores.get(name)
            row[f"score:{name}"] = entry.get("score") if entry else None
            row[f"passed:{name}"] = entry.get("passed") if entry else None
        writer.writerow(row)
    return buffer.getvalue()


def export_md(meta: dict[str, Any], records: list[ResultRecord]) -> str:
    lines = [f"# evaling run `{meta['id']}`", ""]
    if meta.get("label"):
        lines += [f"**Label:** {meta['label']}", ""]

    overall = (meta.get("aggregates") or {}).get("overall")
    counts = meta.get("counts") or {}
    totals = meta.get("totals") or {}
    if overall:
        lines += [
            f"**Overall:** score {overall['score']:.3f}, "
            f"pass rate {overall['pass_rate']:.1%} "
            f"({counts.get('total', overall['cases'])} cells, "
            f"{overall['errors']} errors, "
            f"${totals.get('cost_usd', 0) or 0:.4f})",
            "",
        ]

    gate = meta.get("gate")
    if gate:
        verdict = "✅ passed" if gate["passed"] else "❌ failed"
        lines += [f"**Gate:** {verdict}", ""]
        for check in gate["checks"]:
            mark = "✅" if check["passed"] else "❌"
            lines.append(f"- {mark} `{check['name']}`: {check['detail']}")
        lines.append("")

    matrix = (meta.get("aggregates") or {}).get("matrix") or []
    if matrix:
        lines += [
            "| Variant | Model | Score | Pass rate | Cases | Errors |",
            "|---|---|---|---|---|---|",
        ]
        for cell in matrix:
            lines.append(
                f"| {cell['variant']} | {cell['model']} | {cell['score']:.3f} "
                f"| {cell['pass_rate']:.1%} | {cell['cases']} | {cell['errors']} |"
            )
        lines.append("")

    failures = [record for record in records if not cell_summary(record)[1]]
    if failures:
        lines += [f"## Failures ({len(failures)})", ""]
        for record in failures[:20]:
            key = f"{record.variant} × {record.model} × {record.case_id}"
            if record.error:
                lines.append(f"- **{key}** — error: {record.error}")
            else:
                failed = [
                    f"{name} ({entry.get('detail') or entry.get('error') or 'failed'})"
                    for name, entry in record.scores.items()
                    if entry.get("passed") is not True
                ]
                lines.append(f"- **{key}** — failed criteria: {'; '.join(failed)}")
        if len(failures) > 20:
            lines.append(f"- … and {len(failures) - 20} more")
        lines.append("")
    return "\n".join(lines)
