"""Terminal rendering for the CLI: tables and formatting helpers."""

from typing import Any

from rich.table import Table

from evaling.scoring import cell_summary, filter_failures
from evaling.storage import ResultRecord


def pct(value: float) -> str:
    return f"{value:.1%}"


def score3(value: float) -> str:
    return f"{value:.3f}"


def snip(text: str | None, width: int = 60) -> str:
    if text is None:
        return ""
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def matrix_table(aggregates: dict[str, Any]) -> Table:
    table = Table(header_style="bold", title=None)
    for column in ("Variant", "Model", "Score", "Pass rate", "Cases", "Errors"):
        table.add_column(column, justify="right" if column not in ("Variant", "Model") else "left")
    for cell in aggregates.get("matrix", []):
        errors = cell["errors"]
        table.add_row(
            cell["variant"],
            cell["model"],
            score3(cell["score"]),
            pct(cell["pass_rate"]),
            str(cell["cases"]),
            f"[red]{errors}[/red]" if errors else "0",
        )
    overall = aggregates.get("overall")
    if overall and aggregates.get("matrix") and len(aggregates["matrix"]) > 1:
        table.add_section()
        table.add_row(
            "[bold]overall[/bold]",
            "",
            score3(overall["score"]),
            pct(overall["pass_rate"]),
            str(overall["cases"]),
            str(overall["errors"]),
        )
    return table


def runs_table(runs: list[dict[str, Any]]) -> Table:
    table = Table(header_style="bold")
    for column in ("Run", "Label", "Status", "Started", "Score", "Pass rate", "Cost"):
        table.add_column(column)
    for meta in runs:
        overall = (meta.get("aggregates") or {}).get("overall") or {}
        totals = meta.get("totals") or {}
        cost = totals.get("cost_usd")
        table.add_row(
            meta["id"],
            meta.get("label") or "",
            meta["status"],
            meta.get("started_at") or "",
            score3(overall["score"]) if overall else "",
            pct(overall["pass_rate"]) if overall else "",
            f"${cost:.4f}" if cost is not None else "",
        )
    return table


def case_table(records: list[ResultRecord]) -> Table:
    table = Table(header_style="bold")
    for column in ("Variant", "Model", "Passed", "Score", "Output / error"):
        table.add_column(column)
    for record in sorted(records, key=lambda r: (r.variant, r.model)):
        score, passed = cell_summary(record)
        body = f"[red]{snip(record.error)}[/red]" if record.error else snip(record.output)
        table.add_row(
            record.variant,
            record.model,
            "[green]yes[/green]" if passed else "[red]no[/red]",
            score3(score),
            body,
        )
    return table


def failure_lines(records: list[ResultRecord]) -> list[str]:
    lines = []
    for record in filter_failures(records):
        key = f"[bold]{record.variant} × {record.model} × {record.case_id}[/bold]"
        if record.error:
            lines.append(f"{key} — [red]error:[/red] {snip(record.error, 100)}")
        else:
            failed = [
                f"{name}: {entry.get('detail') or entry.get('error') or 'failed'}"
                for name, entry in record.scores.items()
                if entry.get("passed") is not True
            ]
            lines.append(f"{key} — [red]failed:[/red] {snip('; '.join(failed), 100)}")
    return lines


def gate_lines(gate: dict[str, Any]) -> list[str]:
    verdict = "[green]gate passed[/green]" if gate["passed"] else "[red]gate FAILED[/red]"
    lines = [verdict]
    for check in gate["checks"]:
        mark = "[green]✓[/green]" if check["passed"] else "[red]✗[/red]"
        lines.append(f"  {mark} {check['name']}: {check['detail']}")
    return lines


def compare_table(diff: dict[str, Any]) -> tuple[Table, list[str]]:
    """Render a core compare_aggregates() diff as a table plus notes."""
    table = Table(header_style="bold")
    for column in ("Variant", "Model", "Score", "Δ score", "Pass rate", "Δ pass rate"):
        table.add_column(column)
    for cell in diff["cells"]:
        table.add_row(
            cell["variant"],
            cell["model"],
            f"{score3(cell['score_a'])} → {score3(cell['score_b'])}",
            _delta(cell["score_delta"], score3),
            f"{pct(cell['pass_rate_a'])} → {pct(cell['pass_rate_b'])}",
            _delta(cell["pass_rate_delta"], pct),
        )

    notes = []
    if diff["only_a"]:
        groups = ", ".join(f"{c['variant']}×{c['model']}" for c in diff["only_a"])
        notes.append(f"only in first run: {groups}")
    if diff["only_b"]:
        groups = ", ".join(f"{c['variant']}×{c['model']}" for c in diff["only_b"])
        notes.append(f"only in second run: {groups}")
    return table, notes


def _delta(value: float, fmt) -> str:
    if abs(value) < 1e-9:
        return "="
    color = "green" if value > 0 else "red"
    sign = "+" if value > 0 else ""
    return f"[{color}]{sign}{fmt(value)}[/{color}]"
