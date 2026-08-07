"""The HTML report must be self-contained and safe with untrusted model text."""

import re

import pytest
from click.testing import CliRunner

from evaling.cli import main
from evaling.engine import run_eval
from evaling.export import export_run
from evaling.report import render_compare_html, render_run_html
from evaling.scoring import compare_aggregates
from evaling.storage import RunStore
from helpers import make_config, make_settings

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}

# Anything that would make the file depend on the network, or execute.
EXTERNAL = re.compile(r"""(src|href)\s*=\s*['"]?(https?:)?//""", re.IGNORECASE)


@pytest.fixture
def finished(tmp_path):
    config = make_config(
        tmp_path,
        cases=[
            {"id": "good", "vars": {"q": "alpha"}, "expected": "alpha"},
            {"id": "bad", "vars": {"q": "beta"}, "expected": "NOPE"},
        ],
    )
    config.thresholds.min_pass_rate = 0.9
    settings = make_settings(tmp_path)
    result = run_eval(config, settings, label="report-test")
    store = RunStore(settings.output_dir)
    return store.load_meta(result.run_id), store.load_results(result.run_id)


def test_report_is_self_contained(finished):
    html = render_run_html(*finished)
    assert html.startswith("<!doctype html>")
    assert "<style>" in html
    # no remote assets, no scripts at all
    assert not EXTERNAL.search(html), "report must not reference external resources"
    assert "<script" not in html.lower()
    assert "http://" not in html and "https://" not in html


def test_report_contains_the_run(finished):
    meta, records = finished
    html = render_run_html(meta, records)
    assert meta["id"] in html
    assert "report-test" in html
    assert "Gate failed" in html  # 50% pass rate vs 90% required
    assert "min_pass_rate" in html
    assert "alpha" in html  # the passing output
    assert "expected &#x27;NOPE&#x27;" in html  # criterion detail, escaped
    assert "<h3>good</h3>" in html and "<h3>bad</h3>" in html


def test_failing_cases_sort_first(finished):
    html = render_run_html(*finished)
    assert html.index("<h3>bad</h3>") < html.index("<h3>good</h3>")


def test_all_pass_case_marked_for_css_filter(finished):
    html = render_run_html(*finished)
    # the CSS-only "failures only" toggle keys off these classes
    assert "class='case all-pass'" in html
    assert "#failures-only:checked" in html


def test_hostile_model_output_is_escaped(tmp_path):
    payload = "<script>alert('xss')</script><img src=x onerror=alert(1)>"
    config = make_config(
        tmp_path,
        models=[{"id": "m1", "provider": "mock", "params": {"response": payload}}],
        cases=[{"id": "c1", "vars": {"q": "x"}, "expected": payload}],
    )
    settings = make_settings(tmp_path)
    result = run_eval(config, settings)
    store = RunStore(settings.output_dir)
    html = render_run_html(store.load_meta(result.run_id), store.load_results(result.run_id))

    # The payload survives as visible text, but no live tag is ever produced:
    # every "<" from model output is escaped, so there is no <script> or <img>
    # for a browser to execute.
    assert "<script" not in html.lower()
    assert "<img" not in html.lower()
    assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_hostile_names_are_escaped(tmp_path):
    # variant/model/case identifiers come from config, which is also untrusted
    # input as far as the report is concerned
    config = make_config(
        tmp_path,
        variants=[{"name": "<b>v1</b>", "prompt": [{"role": "user", "content": "{{ q }}"}]}],
        cases=[{"id": "<i>c1</i>", "vars": {"q": "x"}, "expected": "x"}],
    )
    settings = make_settings(tmp_path)
    result = run_eval(config, settings)
    store = RunStore(settings.output_dir)
    html = render_run_html(store.load_meta(result.run_id), store.load_results(result.run_id))
    assert "<b>v1</b>" not in html
    assert "&lt;b&gt;v1&lt;/b&gt;" in html
    assert "&lt;i&gt;c1&lt;/i&gt;" in html


def test_a_hostile_error_message_is_escaped(tmp_path):
    """No test rendered an erroring cell, so this `esc()` was unguarded.

    Provider errors quote what the endpoint said back, and a gateway can put
    anything in a body — this is untrusted text on the same footing as model
    output, and it lands in a different branch of the same function.
    """
    payload = "<script>alert('boom')</script>"
    config = make_config(tmp_path, cases=[{"id": "c1", "vars": {"q": "x"}, "expected": "x"}])
    settings = make_settings(tmp_path)
    result = run_eval(config, settings)
    store = RunStore(settings.output_dir)
    records = store.load_results(result.run_id)
    # Set on the record rather than provoked from a provider: what matters is
    # that the error *branch* escapes, and no mock error carries chosen text.
    for record in records:
        record.error = f"provider said: {payload}"
        record.output = None
    html = render_run_html(store.load_meta(result.run_id), records)
    assert "<script" not in html.lower()
    assert "&lt;script&gt;" in html


def test_a_hostile_run_label_is_escaped(tmp_path):
    """The label is chosen at the command line and lands in the page header."""
    config = make_config(tmp_path, cases=[{"id": "c1", "vars": {"q": "x"}, "expected": "x"}])
    settings = make_settings(tmp_path)
    result = run_eval(config, settings, label="<script>alert('label')</script>")
    store = RunStore(settings.output_dir)
    html = render_run_html(store.load_meta(result.run_id), store.load_results(result.run_id))
    assert "<script" not in html.lower()
    assert "&lt;script&gt;" in html


def test_a_hostile_model_id_is_escaped(tmp_path):
    config = make_config(
        tmp_path,
        models=[{"id": "<script>alert('m')</script>", "provider": "mock"}],
        cases=[{"id": "c1", "vars": {"q": "x"}, "expected": "x"}],
    )
    settings = make_settings(tmp_path)
    result = run_eval(config, settings)
    store = RunStore(settings.output_dir)
    html = render_run_html(store.load_meta(result.run_id), store.load_results(result.run_id))
    assert "<script" not in html.lower()
    assert "&lt;script&gt;" in html


def test_media_referenced_by_hash_not_embedded(tmp_path):
    (tmp_path / "pic.png").write_bytes(b"binary-image-bytes")
    config = make_config(
        tmp_path,
        variants=[
            {
                "name": "v1",
                "prompt": [{"role": "user", "content": [{"text": "look"}, {"image": "pic.png"}]}],
            }
        ],
        cases=[{"id": "c1", "vars": {"q": "x"}}],
    )
    settings = make_settings(tmp_path)
    result = run_eval(config, settings)
    store = RunStore(settings.output_dir)
    html = render_run_html(store.load_meta(result.run_id), store.load_results(result.run_id))
    assert "sha256:" in html
    assert "image/png" in html
    assert "binary-image-bytes" not in html  # never inlined


def test_export_run_html_format(finished):
    meta, records = finished
    assert export_run(meta, records, "html").startswith("<!doctype html>")


def test_compare_html(tmp_path):
    settings = make_settings(tmp_path)
    good = make_config(tmp_path, cases=[{"id": "c1", "vars": {"q": "a"}, "expected": "a"}])
    worse = make_config(tmp_path, cases=[{"id": "c1", "vars": {"q": "a"}, "expected": "NOPE"}])
    run_a = run_eval(good, settings)
    run_b = run_eval(worse, settings)
    store = RunStore(settings.output_dir)
    meta_a, meta_b = store.load_meta(run_a.run_id), store.load_meta(run_b.run_id)

    html = render_compare_html(
        meta_a, meta_b, compare_aggregates(meta_a["aggregates"], meta_b["aggregates"])
    )
    assert html.startswith("<!doctype html>")
    assert not EXTERNAL.search(html)
    assert meta_a["id"] in html and meta_b["id"] in html
    assert "1.000 → 0.000" in html
    assert "delta down" in html  # regression styled as such


class TestCli:
    def cli(self, tmp_path, *args):
        base = ["-o", str(tmp_path / "runs"), "--cache-dir", str(tmp_path / "cache")]
        return CliRunner().invoke(main, base + list(args), env=ENV, catch_exceptions=False)

    def config_file(self, tmp_path):
        path = tmp_path / "eval.yaml"
        path.write_text(
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n"
            "  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: [{id: c1, vars: {q: alpha}, expected: alpha}]\n"
            "scorecard: [{criterion: acc, scorer: {type: exact}}]\n",
            encoding="utf-8",
        )
        return path

    def test_run_html_flag(self, tmp_path):
        out = tmp_path / "report.html"
        result = self.cli(tmp_path, "run", str(self.config_file(tmp_path)), "--html", str(out))
        assert result.exit_code == 0, result.output
        assert "report written to" in result.output
        assert out.read_text(encoding="utf-8").startswith("<!doctype html>")

    def test_export_html_to_file(self, tmp_path):
        self.cli(tmp_path, "-q", "run", str(self.config_file(tmp_path)))
        out = tmp_path / "exported.html"
        result = self.cli(tmp_path, "export", "latest", "--format", "html", "--out", str(out))
        assert result.exit_code == 0, result.output
        assert "<!doctype html>" in out.read_text(encoding="utf-8")

    def test_compare_html_flag(self, tmp_path):
        config = self.config_file(tmp_path)
        self.cli(tmp_path, "-q", "run", str(config))
        self.cli(tmp_path, "-q", "run", str(config), "--no-cache")
        out = tmp_path / "diff.html"
        runs = self.cli(tmp_path, "--json", "list").output
        import json as jsonlib

        ids = [run["id"] for run in jsonlib.loads(runs)]
        result = self.cli(tmp_path, "compare", ids[1], ids[0], "--html", str(out))
        assert result.exit_code == 0, result.output
        assert out.read_text(encoding="utf-8").startswith("<!doctype html>")


class TestLayout:
    """Spacing bugs are invisible to assertions about content.

    The totals line under the summary table carried a negative top margin,
    which pulled it up over the table's last row — the text and the row's
    bottom border rendered on top of each other.
    """

    def test_totals_line_does_not_pull_itself_up_into_the_table(self):
        from evaling.report import STYLE

        rule = next(line for line in STYLE.splitlines() if line.startswith(".totals"))
        margin = rule.split("margin:")[1].split(";")[0].strip()
        top = margin.split()[0]
        assert not top.startswith("-"), f".totals has a negative top margin ({top})"

    def test_no_negative_top_margins_anywhere_in_the_stylesheet(self):
        from evaling.report import STYLE

        offenders = []
        for line in STYLE.splitlines():
            if "margin:" not in line:
                continue
            value = line.split("margin:")[1].split(";")[0].strip()
            if value.split() and value.split()[0].startswith("-"):
                offenders.append(line.strip())
        assert not offenders, f"negative top margins can overlap content: {offenders}"
