"""Self-contained HTML reports.

One file, no external requests: styles are inline and interactivity is
CSS-only (checkbox + sibling selectors), so a report opens from disk, survives
any Content-Security-Policy, and works with no network.

Everything that originates from a model, a config, or a case is untrusted and
goes through ``esc()``.
"""

from html import escape
from typing import Any

from evaling.scoring import cell_summary, selection_note
from evaling.storage import ResultRecord

STYLE = """
:root {
  --bg: #ffffff; --fg: #1a1a1a; --muted: #667085; --line: #e4e7ec;
  --panel: #f9fafb; --pass: #067647; --pass-bg: #ecfdf3;
  --fail: #b42318; --fail-bg: #fef3f2; --accent: #175cd3;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16181d; --fg: #e6e8eb; --muted: #98a2b3; --line: #2b2f38;
    --panel: #1d2027; --pass: #75e0a7; --pass-bg: #0b2a1c;
    --fail: #fda29b; --fail-bg: #2d1513; --accent: #84adff;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.5rem 4rem; background: var(--bg); color: var(--fg);
  font: 15px/1.5 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
h2 { font-size: 1.1rem; margin: 2.5rem 0 .75rem; }
.sub { color: var(--muted); margin: 0 0 1.5rem; font-size: .9rem; }
code, pre, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.banner {
  padding: .75rem 1rem; border-radius: 8px; margin: 0 0 1.5rem; font-weight: 600;
  border: 1px solid transparent;
}
.banner.pass { background: var(--pass-bg); color: var(--pass); border-color: var(--pass); }
.banner.fail { background: var(--fail-bg); color: var(--fail); border-color: var(--fail); }
.banner ul { margin: .5rem 0 0; padding-left: 1.2rem; font-weight: 400; }
.totals { color: var(--muted); font-size: .9rem; margin: .75rem 0 1.5rem; }
.warn { border-left: 3px solid #b45309; background: rgba(180,83,9,.08);
        padding: .6rem .8rem; margin: .75rem 0; font-size: .9rem; }
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
th, td { text-align: left; padding: .5rem .7rem; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.overall td { font-weight: 600; border-top: 2px solid var(--line); }
.scroll { overflow-x: auto; }
.filter { margin: 1rem 0; color: var(--muted); font-size: .9rem; }
.notice { border-left: 3px solid var(--muted); background: rgba(127,127,127,.08);
          padding: .75rem 1rem; margin: 1rem 0; font-size: .9rem; }
.case {
  border: 1px solid var(--line); border-radius: 10px; margin: 0 0 1rem;
  overflow: hidden; background: var(--panel);
}
.case > h3 {
  margin: 0; padding: .6rem .9rem; font-size: .95rem; background: var(--bg);
  border-bottom: 1px solid var(--line);
}
.cell { padding: .8rem .9rem; border-top: 1px solid var(--line); }
.cell:first-of-type { border-top: none; }
.cell > .head { display: flex; gap: .6rem; align-items: baseline; flex-wrap: wrap; }
.badge {
  font-size: .75rem; font-weight: 700; padding: .1rem .45rem; border-radius: 5px;
  text-transform: uppercase; letter-spacing: .03em;
}
.badge.pass { background: var(--pass-bg); color: var(--pass); }
.badge.fail { background: var(--fail-bg); color: var(--fail); }
.who { font-weight: 600; }
.meta { color: var(--muted); font-size: .82rem; margin-left: auto; }
.out {
  white-space: pre-wrap; word-break: break-word; margin: .6rem 0 0; padding: .6rem .7rem;
  background: var(--bg); border: 1px solid var(--line); border-radius: 6px;
  font-size: .85rem; max-height: 22rem; overflow: auto;
}
.err { color: var(--fail); }
.criteria { list-style: none; margin: .6rem 0 0; padding: 0; font-size: .85rem; }
.criteria li { padding: .15rem 0; }
.criteria .name { font-weight: 600; }
.criteria .why { color: var(--muted); }
details { margin: .6rem 0 0; font-size: .85rem; }
summary { cursor: pointer; color: var(--accent); }
.turn { margin: .5rem 0 0; }
.turn .role {
  font-size: .72rem; text-transform: uppercase; letter-spacing: .04em; color: var(--muted);
}
.media { color: var(--muted); font-style: italic; }
.empty { color: var(--muted); font-style: italic; }
/* CSS-only "failures only": no JavaScript anywhere in this file. */
#failures-only { position: absolute; opacity: 0; pointer-events: none; }
#failures-only:checked ~ .cases .cell.pass { display: none; }
#failures-only:checked ~ .cases .case.all-pass { display: none; }
.filter label { cursor: pointer; user-select: none; }
.filter label::before {
  content: "☐ "; font-size: 1rem;
}
#failures-only:checked ~ .filter label::before { content: "☑ "; }
.delta.up { color: var(--pass); }
.delta.down { color: var(--fail); }
"""


def esc(value: Any) -> str:
    """Escape anything untrusted (model output, config names, case data)."""
    return escape("" if value is None else str(value), quote=True)


def _pct(value: float) -> str:
    return f"{value:.1%}"


def _score(value: float) -> str:
    return f"{value:.3f}"


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title><style>{STYLE}</style></head>"
        f'<body><div class="wrap">{body}</div></body></html>\n'
    )


def _gate_banner(gate: dict[str, Any] | None) -> str:
    if not gate:
        return ""
    passed = gate.get("passed")
    checks = "".join(
        f"<li>{'✓' if check.get('passed') else '✗'} "
        f"<strong>{esc(check.get('name'))}</strong>: {esc(check.get('detail'))}</li>"
        for check in gate.get("checks", [])
    )
    label = "Gate passed" if passed else "Gate failed"
    return (
        f'<div class="banner {"pass" if passed else "fail"}">{label}'
        f"{f'<ul>{checks}</ul>' if checks else ''}</div>"
    )


def _matrix_table(aggregates: dict[str, Any]) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{esc(cell['variant'])}</td><td>{esc(cell['model'])}</td>"
        f"<td class='num'>{_score(cell['score'])}</td>"
        f"<td class='num'>{_pct(cell['pass_rate'])}</td>"
        f"<td class='num'>{esc(cell['cases'])}</td>"
        f"<td class='num'>{esc(cell['errors'] or '')}</td>"
        "</tr>"
        for cell in aggregates.get("matrix", [])
    )
    overall = aggregates.get("overall")
    if overall and len(aggregates.get("matrix", [])) > 1:
        rows += (
            "<tr class='overall'><td>overall</td><td></td>"
            f"<td class='num'>{_score(overall['score'])}</td>"
            f"<td class='num'>{_pct(overall['pass_rate'])}</td>"
            f"<td class='num'>{esc(overall['cases'])}</td>"
            f"<td class='num'>{esc(overall['errors'] or '')}</td></tr>"
        )
    return (
        "<div class='scroll'><table><thead><tr>"
        "<th>Variant</th><th>Model</th><th class='num'>Score</th>"
        "<th class='num'>Pass rate</th><th class='num'>Cases</th><th class='num'>Errors</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _messages_block(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return ""
    turns = []
    for message in messages:
        parts = []
        for part in message.get("parts", []):
            if part.get("type") == "text":
                parts.append(f"<div class='out'>{esc(part.get('text'))}</div>")
            else:
                # Binary content is referenced by hash, never embedded.
                parts.append(
                    f"<div class='media'>[{esc(part.get('type'))} "
                    f"{esc(part.get('media_type'))} "
                    f"sha256:{esc((part.get('sha256') or '')[:12])}]</div>"
                )
        turns.append(
            f"<div class='turn'><div class='role'>{esc(message.get('role'))}</div>"
            f"{''.join(parts)}</div>"
        )
    return "<details><summary>Prompt sent</summary>" + "".join(turns) + "</details>"


def _criteria_list(record: ResultRecord) -> str:
    if not record.scores:
        return ""
    items = []
    for name, entry in record.scores.items():
        ok = entry.get("passed") is True
        why = entry.get("detail") or entry.get("error") or ""
        items.append(
            f"<li>{'✓' if ok else '✗'} <span class='name'>{esc(name)}</span> "
            f"<span class='mono'>{_score(float(entry.get('score') or 0))}</span>"
            + (f" <span class='why'>— {esc(why)}</span>" if why else "")
            + "</li>"
        )
    return f"<ul class='criteria'>{''.join(items)}</ul>"


def _cell(record: ResultRecord, no_look: bool = False) -> str:
    score, passed = cell_summary(record)
    bits = []
    if record.cached:
        bits.append("cached")
    if record.latency_ms is not None:
        bits.append(f"{record.latency_ms:.0f} ms")
    if record.input_tokens or record.output_tokens:
        bits.append(esc(f"{record.input_tokens or 0}/{record.output_tokens or 0} tok"))
    if record.cost_usd:
        bits.append(f"${record.cost_usd:.4f}")

    if record.error:
        body = f"<div class='out err'>{esc(record.error)}</div>"
    elif record.output:
        body = f"<div class='out'>{esc(record.output)}</div>"
    else:
        # No error and no output means one of two different things, and
        # "(empty response)" said the wrong one for a whole no-look run: the
        # model answered, the answer was never stored. The record cannot tell
        # them apart — redaction and an empty prompt both leave `messages`
        # empty — so the run says which it was.
        body = (
            "<div class='out empty'>(withheld — no-look run)</div>"
            if no_look
            else "<div class='out empty'>(empty response)</div>"
        )

    state = "pass" if passed else "fail"
    return (
        f"<div class='cell {state}'>"
        "<div class='head'>"
        f"<span class='badge {state}'>{state}</span>"
        f"<span class='who'>{esc(record.variant)} × {esc(record.model)}</span>"
        f"<span class='mono'>{_score(score)}</span>"
        f"<span class='meta'>{esc(' · '.join(bits))}</span>"
        "</div>"
        f"{body}{_criteria_list(record)}{_messages_block(record.messages)}"
        "</div>"
    )


def _cases_section(records: list[ResultRecord], no_look: bool = False) -> str:
    grouped: dict[str, list[ResultRecord]] = {}
    for record in records:
        grouped.setdefault(record.case_id, []).append(record)

    # Failing cases first, then by id — the reader's attention goes where the
    # problems are.
    def sort_key(item: tuple[str, list[ResultRecord]]) -> tuple[int, str]:
        case_id, cells = item
        all_pass = all(cell_summary(cell)[1] for cell in cells)
        return (1 if all_pass else 0, case_id)

    sections = []
    for case_id, cells in sorted(grouped.items(), key=sort_key):
        ordered = sorted(cells, key=lambda r: (cell_summary(r)[1], r.variant, r.model))
        all_pass = all(cell_summary(cell)[1] for cell in cells)
        sections.append(
            f"<section class='case{' all-pass' if all_pass else ''}'>"
            f"<h3>{esc(case_id)}</h3>"
            f"{''.join(_cell(record, no_look) for record in ordered)}"
            "</section>"
        )
    return "".join(sections)


#: Above this many cells the per-case drill-down is limited to failures.
#: A full drill-down costs roughly 1.5 KB of HTML per cell, so an unbounded
#: report of a large run is a file no browser will open — 50k cells produced a
#: 75 MB page. Aggregates and the gate are always complete; only the
#: cell-by-cell detail is trimmed.
MAX_DETAILED_CASES = 2_000

#: How many failing cases to show once a report is in summary mode.
SUMMARY_MODE_FAILURES = 200


def _summary_notice(shown: int, total: int, failures: int) -> str:
    hidden = total - shown
    return (
        "<div class='notice'><strong>Large run — showing partial detail.</strong> "
        f"This run has {total:,} cells, so the drill-down below covers "
        f"{shown:,} of them ({failures:,} failing) and omits {hidden:,}. "
        "The summary and gate above cover the whole run. "
        "For everything, use <span class='mono'>evaling export &lt;run&gt; --format csv</span> "
        "or <span class='mono'>evaling show &lt;run&gt; --case &lt;id&gt;</span>.</div>"
    )


def _select_for_detail(records: list[ResultRecord]) -> tuple[list[ResultRecord], str]:
    """Which records to render in full, plus a notice when that isn't all of them."""
    if len(records) <= MAX_DETAILED_CASES:
        return records, ""
    failures = [record for record in records if not cell_summary(record)[1]]
    shown = failures[:SUMMARY_MODE_FAILURES]
    return shown, _summary_notice(len(shown), len(records), len(failures))


def render_run_html(meta: dict[str, Any], records: list[ResultRecord]) -> str:
    """A complete, self-contained report for one run."""
    label = f" — {esc(meta.get('label'))}" if meta.get("label") else ""
    counts = meta.get("counts") or {}
    totals = meta.get("totals") or {}
    cost = totals.get("cost_usd") or 0.0
    totals_line = (
        f"{counts.get('succeeded', 0)}/{counts.get('total', len(records))} succeeded"
        f" · {counts.get('failed', 0)} failed"
        f" · {counts.get('cached', 0)} cached"
        f" · {totals.get('input_tokens', 0)} in / {totals.get('output_tokens', 0)} out tokens"
        f" · ${cost:.4f}"
    )
    aggregates = meta.get("aggregates") or {}
    detailed, notice = _select_for_detail(records)

    body = (
        f"<h1>evaling run <span class='mono'>{esc(meta.get('id'))}</span>{label}</h1>"
        f"<p class='sub'>{esc(meta.get('status'))}"
        f" · started {esc(meta.get('started_at'))}"
        + (f" · finished {esc(meta.get('finished_at'))}" if meta.get("finished_at") else "")
        + "</p>"
        + _gate_banner(meta.get("gate"))
        + (f"<h2>Summary</h2>{_matrix_table(aggregates)}" if aggregates else "")
        + f"<p class='totals'>{esc(totals_line)}</p>"
        + "<h2>Cases</h2>"
        + notice
        + "<input type='checkbox' id='failures-only'>"
        + "<div class='filter'><label for='failures-only'>Show failures only</label></div>"
        + f"<div class='cases'>{_cases_section(detailed, bool(meta.get('no_look')))}</div>"
    )
    return _page(f"evaling run {meta.get('id')}", body)


def render_compare_html(
    meta_a: dict[str, Any], meta_b: dict[str, Any], diff: dict[str, Any]
) -> str:
    """A self-contained side-by-side comparison of two runs."""

    def delta_cell(value: float, fmt) -> str:
        if abs(value) < 1e-9:
            return "<td class='num'>=</td>"
        css = "up" if value > 0 else "down"
        return f"<td class='num delta {css}'>{'+' if value > 0 else ''}{fmt(value)}</td>"

    rows = "".join(
        "<tr>"
        f"<td>{esc(cell['variant'])}</td><td>{esc(cell['model'])}</td>"
        f"<td class='num'>{_score(cell['score_a'])} → {_score(cell['score_b'])}</td>"
        + delta_cell(cell["score_delta"], _score)
        + f"<td class='num'>{_pct(cell['pass_rate_a'])} → {_pct(cell['pass_rate_b'])}</td>"
        + delta_cell(cell["pass_rate_delta"], _pct)
        + "</tr>"
        for cell in diff.get("cells", [])
    )
    notes = []
    for key, text in (("only_a", "Only in the first run"), ("only_b", "Only in the second run")):
        groups = diff.get(key) or []
        if groups:
            listed = ", ".join(f"{esc(g['variant'])}×{esc(g['model'])}" for g in groups)
            notes.append(f"<p class='totals'>{text}: {listed}</p>")

    overall = diff["overall"]
    # Above the table, not below it. This file is the artifact people share,
    # and a caveat that arrives after the numbers arrives too late to change
    # how they are read.
    caveat = selection_note(meta_a, meta_b)
    warning = f"<p class='warn'>{esc(caveat)}</p>" if caveat else ""
    body = (
        f"<h1>evaling compare</h1>"
        f"<p class='sub'><span class='mono'>{esc(meta_a.get('id'))}</span> → "
        f"<span class='mono'>{esc(meta_b.get('id'))}</span></p>"
        + warning
        + "<div class='scroll'><table><thead><tr>"
        "<th>Variant</th><th>Model</th><th class='num'>Score</th><th class='num'>Δ</th>"
        "<th class='num'>Pass rate</th><th class='num'>Δ</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
        + "".join(notes)
        + f"<p class='totals'>Overall: score {_score(overall['score_a'])} → "
        f"{_score(overall['score_b'])}, pass rate {_pct(overall['pass_rate_a'])} → "
        f"{_pct(overall['pass_rate_b'])}</p>"
    )
    return _page("evaling compare", body)
