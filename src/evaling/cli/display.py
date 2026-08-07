"""Terminal rendering for the CLI: tables and formatting helpers."""

from typing import Any

from rich.console import Group
from rich.markup import escape as markup_escape
from rich.table import Table
from rich.text import Text

from evaling.scoring import cell_summary, filter_failures
from evaling.storage import ResultRecord


def pct(value: float) -> str:
    return f"{value:.1%}"


def score3(value: float) -> str:
    return f"{value:.3f}"


def snip(text: str | None, width: int = 60) -> str:
    """Flatten, truncate, and neutralize rich markup in untrusted text.

    Model output and provider errors are not ours: a response containing
    "[/bold]" would otherwise raise MarkupError and crash the command, and
    "[red]" would silently restyle the terminal.
    """
    if text is None:
        return ""
    flat = " ".join(text.split())
    if len(flat) > width:
        flat = flat[: width - 1] + "…"
    return markup_escape(flat)


def safe(value: Any) -> str:
    """Escape a config-supplied value (variant/model/case name, label)."""
    return markup_escape("" if value is None else str(value))


#: Lines kept per prompt message in a verbose cell block. The prompt is nearly
#: identical on every cell of a variant, so past a screenful it is repetition.
PROMPT_LINES = 20

#: Lines kept from the response. Generous, because reading the response is what
#: verbose is for; the cap is here for a runaway generation, not for prose.
OUTPUT_LINES = 200

#: Character backstop per part. A response with no newlines counts as one line
#: however long it is, so a line cap alone does not bound the screen.
PROMPT_CHARS = PROMPT_LINES * 100
OUTPUT_CHARS = OUTPUT_LINES * 100


def _clip(text: str, max_lines: int, max_chars: int) -> tuple[str, str | None]:
    """Text bounded by both line count and length, plus a note on what was cut.

    Both bounds can fire on the same text, and both are reported — saying only
    that it was cut at a character count would hide how much else there was.
    """
    lines = text.splitlines() or [""]
    dropped = len(lines) - max_lines
    if dropped > 0:
        lines = lines[:max_lines]
    body = "\n".join(lines)
    notes = [f"{dropped} more lines"] if dropped > 0 else []
    if len(body) > max_chars:
        body = body[:max_chars]
        notes.append(f"truncated at {max_chars} characters")
    return body, f"… {', '.join(notes)}" if notes else None


def _part_text(part: dict[str, Any]) -> str:
    """One serialized message part as text. Media is described, never inlined."""
    if part.get("type") == "text":
        return str(part.get("text", ""))
    digest = str(part.get("sha256", ""))[:12]
    return f"[{part.get('type')} {part.get('media_type', '?')} sha256:{digest}]"


def _cell_header(record: ResultRecord) -> str:
    score, passed = cell_summary(record)
    if record.error:
        verdict = "[red]ERROR[/red]"
    elif passed:
        verdict = f"[green]PASS[/green] {score3(score)}"
    else:
        verdict = f"[red]FAIL[/red] {score3(score)}"
    facts = []
    if record.cached:
        # Said plainly: an instant run with no explanation reads as a no-op.
        # Alone, with neither latency nor cost — a cached cell made no call,
        # so it timed nothing and paid nothing. The record keeps the cost the
        # original call had, which belongs in the run's tally but reads here
        # as money spent on the line that just said nothing was spent.
        facts.append("cached")
    else:
        if record.latency_ms is not None:
            facts.append(f"{record.latency_ms:.0f}ms")
        if record.cost_usd:
            facts.append(f"${record.cost_usd:.4f}")
    title = f"{safe(record.variant)} × {safe(record.model)} × {safe(record.case_id)}"
    trailer = f" [dim]· {' · '.join(facts)}[/dim]" if facts else ""
    return f"[bold]{title}[/bold]  {verdict}{trailer}"


def cell_block(record: ResultRecord) -> Group:
    """One cell in full: what was sent, what came back, and how it scored.

    A single renderable, so the caller prints it in one call. Under concurrency
    rich holds its lock for the duration of a print, so a block emitted this
    way is never interleaved with another cell's — two prints per cell would
    be.

    Every untrusted string — model output, provider errors, prompt text — is
    passed as a ``Text`` object rather than a markup string, so there is no
    parse step for it to break. Markup in model output has previously both
    crashed the command and restyled the terminal.
    """
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="dim", justify="right", no_wrap=True)
    grid.add_column(overflow="fold")

    def row(label: str, body: str, note: str | None = None, style: str | None = None) -> None:
        grid.add_row(f"{label} │", Text(body, style=style or ""))
        if note:
            grid.add_row("│", Text(note, style="dim"))

    for message in record.messages:
        text = "\n\n".join(_part_text(part) for part in message.get("parts", []))
        row(str(message.get("role", "?")), *_clip(text, PROMPT_LINES, PROMPT_CHARS))

    if record.error:
        row("error", *_clip(record.error, OUTPUT_LINES, OUTPUT_CHARS), style="red")
    elif record.output is not None:
        row("output", *_clip(record.output, OUTPUT_LINES, OUTPUT_CHARS))
    elif not record.messages:
        # Both gone and no error: no-look stripped them before the record
        # reached any display. Said, rather than rendered as a blank block.
        # Conditioned on the prompt being absent too, so a cell that somehow
        # lost only its output is not labelled with a privacy mode nobody
        # turned on.
        grid.add_row("│", Text("prompt and output withheld (no-look)", style="dim"))

    for name, entry in record.scores.items():
        mark = "pass" if entry.get("passed") else "fail"
        summary = Text(f"{name} {score3(entry.get('score') or 0.0)} {mark}")
        detail = entry.get("detail") or entry.get("error")
        if detail:
            summary.append(f" — {' '.join(str(detail).split())[:200]}", style="dim")
        grid.add_row("scores │", summary)

    # Trailing blank line: consecutive blocks otherwise run together into one
    # wall, which is the failure mode of every verbose flag.
    return Group(_cell_header(record), grid, Text(""))


def matrix_table(aggregates: dict[str, Any]) -> Table:
    table = Table(header_style="bold", title=None)
    for column in ("Variant", "Model", "Score", "Pass rate", "Cases", "Errors"):
        table.add_column(column, justify="right" if column not in ("Variant", "Model") else "left")
    for cell in aggregates.get("matrix", []):
        errors = cell["errors"]
        table.add_row(
            safe(cell["variant"]),
            safe(cell["model"]),
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


def _short_time(stamp: str | None) -> str:
    """`2026-07-30T01:15:32Z` -> `07-30 01:15`.

    A listing is scanned, not parsed; the full stamp is in run.json and in
    `show`. Reclaiming those columns is what keeps the run id whole at 80.
    """
    if not stamp or len(stamp) < 16 or stamp[4] != "-":
        return safe(stamp or "")
    return safe(f"{stamp[5:10]} {stamp[11:16]}")


def runs_table(runs: list[dict[str, Any]]) -> Table:
    table = Table(header_style="bold")
    # The run id is what every other command takes as an argument, so it must
    # stay copyable; rich elides it at 80 columns otherwise. Started can shrink
    # instead — nobody retypes a timestamp.
    # The run id and the cost must not be elided: one is what every other
    # command takes as an argument, and a truncated "$0.00…" reads as roughly
    # nothing when it may be $0.0045. A label can ellipsize; so can a date.
    table.add_column("Run", no_wrap=True, overflow="ignore")
    for column in ("Label", "Status", "Started", "Score", "Pass rate"):
        table.add_column(column, overflow="ellipsis")
    table.add_column("Cost", no_wrap=True, overflow="ignore")
    for meta in runs:
        overall = (meta.get("aggregates") or {}).get("overall") or {}
        totals = meta.get("totals") or {}
        cost = totals.get("cost_usd")
        table.add_row(
            safe(meta["id"]),
            safe(meta.get("label") or ""),
            safe(meta["status"]),
            _short_time(meta.get("started_at")),
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
            safe(record.variant),
            safe(record.model),
            "[green]yes[/green]" if passed else "[red]no[/red]",
            score3(score),
            body,
        )
    return table


def failure_lines(records: list[ResultRecord]) -> list[str]:
    lines = []
    for record in filter_failures(records):
        key = f"[bold]{safe(record.variant)} × {safe(record.model)} × {safe(record.case_id)}[/bold]"
        if record.error:
            lines.append(f"{key} — [red]error:[/red] {snip(record.error, 100)}")
        else:
            failed = [
                f"{name}: {entry.get('detail') or entry.get('error') or 'failed'}"
                for name, entry in record.scores.items()
                if entry.get("passed") is not True
            ]
            # 100 chars truncated an LLM judge's rationale to uselessness.
            lines.append(f"{key} — [red]failed:[/red] {snip('; '.join(failed), 300)}")
    return lines


def gate_lines(gate: dict[str, Any]) -> list[str]:
    verdict = "[green]gate passed[/green]" if gate["passed"] else "[red]gate FAILED[/red]"
    lines = [verdict]
    for check in gate["checks"]:
        mark = "[green]✓[/green]" if check["passed"] else "[red]✗[/red]"
        lines.append(f"  {mark} {safe(check['name'])}: {safe(check['detail'])}")
    return lines


def compare_table(diff: dict[str, Any]) -> tuple[Table, list[str]]:
    """Render a core compare_aggregates() diff as a table plus notes."""
    table = Table(header_style="bold")
    for column in ("Variant", "Model", "Score", "Δ score", "Pass rate", "Δ pass rate"):
        table.add_column(column)
    for cell in diff["cells"]:
        table.add_row(
            safe(cell["variant"]),
            safe(cell["model"]),
            f"{score3(cell['score_a'])} → {score3(cell['score_b'])}",
            _delta(cell["score_delta"], score3),
            f"{pct(cell['pass_rate_a'])} → {pct(cell['pass_rate_b'])}",
            _delta(cell["pass_rate_delta"], pct),
        )

    notes = []
    if diff["only_a"]:
        groups = ", ".join(f"{safe(c['variant'])}×{safe(c['model'])}" for c in diff["only_a"])
        notes.append(f"only in first run: {groups}")
    if diff["only_b"]:
        groups = ", ".join(f"{safe(c['variant'])}×{safe(c['model'])}" for c in diff["only_b"])
        notes.append(f"only in second run: {groups}")
    return table, notes


def _delta(value: float, fmt) -> str:
    if abs(value) < 1e-9:
        return "="
    color = "green" if value > 0 else "red"
    sign = "+" if value > 0 else ""
    return f"[{color}]{sign}{fmt(value)}[/{color}]"


def _mcp_extra_line(install) -> str:
    """Whether `evaling mcp` will start, not merely whether a package is there.

    A 1.x install reported as "installed" reads as a clean bill of health next
    to a server that refuses to start.
    """
    found = install.get("mcp_version")
    if found is None:
        return "not installed"
    if install["mcp_extra"]:
        return f"installed ({found})"
    return f"{found} — too old, needs 2.0 or newer"


def doctor_lines(report, probes=None) -> list[str]:
    """`evaling doctor` as flat text, so it pastes into an issue unmangled.

    Deliberately not a table: this output exists to be copied into a bug
    report, where box-drawing characters and terminal-width wrapping are
    noise. Every value is escaped — a path or a model id can contain
    brackets, and rich would eat them as markup.
    """
    sections = report.sections
    lines: list[str] = []

    install = sections["evaling"]
    lines.append("[bold]evaling[/bold]")
    lines.append(f"  version      {safe(install['version'])}")
    lines.append(f"  python       {safe(install['python'])}  ({safe(install['executable'])})")
    lines.append(f"  platform     {safe(install['platform'])}")
    # Named separately from the interpreter because an MCP client config needs
    # this path and would otherwise be given the wrong one.
    lines.append(f"  evaling      {safe(install['evaling_path'] or 'not on PATH')}")
    lines.append(f"  mcp extra    {safe(_mcp_extra_line(install))}")

    config = sections["config"]
    lines.append("")
    lines.append("[bold]config[/bold]")
    lines.append(f"  path         {safe(config['path'])}")
    if not config["found"]:
        lines.append("  [red]not found[/red]")
    elif config["error"]:
        lines.append(f"  [red]{safe(config['error'])}[/red]")
    else:
        lines.append(f"  models       {safe(', '.join(config['models']))}")
        lines.append(f"  variants     {safe(', '.join(config['variants']))}")
        lines.append(f"  cases        {safe(config['cases'])}")
        lines.append(f"  criteria     {safe(', '.join(config['criteria']))}")
        if config["judges"]:
            lines.append(f"  judges       {safe(', '.join(config['judges']))}")
        if config["no_look"]:
            lines.append("  privacy      no-look is on")

    lines.append("")
    lines.append("[bold]settings[/bold]  (value, and the layer it came from)")
    for name, entry in sections["settings"].items():
        lines.append(f"  {name:<12} {safe(entry['value'])}  [dim]from {safe(entry['from'])}[/dim]")

    secrets = sections["secrets"]
    lines.append("")
    lines.append("[bold]secrets[/bold]")
    if not secrets["files"]:
        lines.append("  none found  [dim](API keys come from the environment)[/dim]")
    for entry in secrets["files"]:
        lines.append(f"  {safe(entry['path'])}")
        if entry["error"]:
            lines.append(f"    [red]{safe(entry['error'])}[/red]")
        else:
            # Names only, never values.
            lines.append(f"    defines  {safe(', '.join(entry['keys']) or '(nothing)')}")

    if sections["models"]:
        lines.append("")
        lines.append("[bold]models[/bold]")
        width = max(len(entry["id"]) for entry in sections["models"])
        for entry in sections["models"]:
            bits = [f"provider {safe(entry['provider'])}", f"role {safe(entry['role'])}"]
            if entry.get("api_key_env"):
                found = "found" if entry.get("api_key_found") else "[red]missing[/red]"
                bits.append(f"{safe(entry['api_key_env'])} {found}")
            if entry.get("base_url"):
                bits.append(f"at {safe(entry['base_url'])}")
            if entry.get("command"):
                bits.append(f"runs {safe(entry['command'])}")
            if entry.get("error"):
                bits.append(f"[red]{safe(entry['error'])}[/red]")
            lines.append(f"  {safe(entry['id']):<{width}}  {' · '.join(bits)}")

    cache, runs = sections["cache"], sections["runs"]
    megabytes = cache["bytes"] / 1_048_576
    lines.append("")
    lines.append("[bold]storage[/bold]")
    lines.append(
        f"  cache        {cache['entries']} entries · {megabytes:.1f} MB · {safe(cache['path'])}"
        + ("" if cache["enabled"] else "  [dim](disabled)[/dim]")
    )
    lines.append(
        f"  runs         {runs['count']} stored · {safe(runs['path'])}"
        + ("" if runs["writable"] else "  [red](not writable)[/red]")
    )
    if runs["baseline"]:
        lines.append(f"  baseline     {safe(runs['baseline'])}")

    if probes is not None:
        lines.append("")
        lines.append("[bold]provider checks[/bold]")
        for entry in probes:
            if entry["reachable"]:
                cost = entry.get("cost_usd")
                spent = f" (${cost:.6f})" if cost else ""
                lines.append(f"  [green]ok[/green]    {safe(entry['id'])}{spent}")
            else:
                lines.append(f"  [red]fail[/red]  {safe(entry['id'])}: {safe(entry['error'])}")

    lines.append("")
    if report.problems:
        lines.append(f"[bold red]{len(report.problems)} problem(s)[/bold red]")
        for problem in report.problems:
            lines.append(f"  [red]•[/red] {safe(problem)}")
    else:
        lines.append("[green]no problems found[/green]")
    return lines
