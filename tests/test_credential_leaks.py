"""Every path a credential could take out of evaling, held shut.

Each of these reproduces a leak that shipped. Keys reach evaling from the
environment or a gitignored secrets file; they must not reach stdout, an
error message, results.jsonl, a report, or a log. The awkward cases are the
ones where the credential arrives back from somewhere else — a gateway
echoing a header, a script printing its own environment — because then the
leak is in text evaling is merely relaying.
"""

import httpx
import pytest
from click.testing import CliRunner

from evaling.cli import main
from evaling.config import Settings, load_config
from evaling.engine import run_eval
from evaling.secrets import describe_secrets
from evaling.storage import RunStore

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}
KEY = "sk-ant-a-real-looking-live-credential"

#: A leaked fragment this long is still a leak. PyYAML truncates its quoted
#: snippet to a window around the error column, so asserting on the whole key
#: passed even when the message carried most of it — which is how the first
#: version of this test managed to pass against the unfixed code.
FRAGMENT = 12


def assert_no_secret(text: str, secret: str = KEY) -> None:
    """Fail if any run of `secret` at least FRAGMENT long appears in `text`."""
    found = [
        secret[start : start + FRAGMENT]
        for start in range(len(secret) - FRAGMENT + 1)
        if secret[start : start + FRAGMENT] in text
    ]
    assert not found, f"credential fragment {found[0]!r} leaked into: {text[:300]!r}"


def settings_for(path, cache=False):
    return Settings.model_validate(
        {
            "output_dir": str(path / "runs"),
            "cache_dir": str(path / "cache"),
            "cache": cache,
            "concurrency": 1,
        }
    )


class TestAMalformedSecretsFileDoesNotQuoteItself:
    """`doctor` output is documented as safe to paste into an issue.

    A YAML syntax error in a secrets file is, by construction, a syntax error
    on a line holding a credential — and PyYAML's message quotes the line it
    failed on.
    """

    @pytest.fixture
    def broken(self, tmp_path):
        (tmp_path / ".evaling.secrets.yaml").write_text(
            f"ANTHROPIC_API_KEY: {KEY}: live\n", encoding="utf-8"
        )
        return tmp_path

    def test_the_value_is_not_in_the_error(self, broken):
        [described] = describe_secrets(broken, env={})
        assert described["error"], "a malformed file should still be reported"
        assert_no_secret(described["error"])

    def test_the_error_still_says_where(self, broken):
        """Useless errors get ignored; the position is what fixes the file."""
        [described] = describe_secrets(broken, env={})
        assert "line 1" in described["error"]

    def test_doctor_does_not_print_it(self, broken):
        (broken / "eval.yaml").write_text(
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: [{id: c1, vars: {q: a}}]\n"
            'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n',
            encoding="utf-8",
        )
        result = CliRunner().invoke(
            main, ["-c", str(broken / "eval.yaml"), "doctor"], env=ENV, catch_exceptions=False
        )
        assert_no_secret(result.output)
        result_json = CliRunner().invoke(
            main,
            ["-c", str(broken / "eval.yaml"), "--json", "doctor"],
            env=ENV,
            catch_exceptions=False,
        )
        assert_no_secret(result_json.output)


class TestAReflectedKeyNeverReachesDisk:
    """A misconfigured gateway that echoes the auth header back at us."""

    def project(self, tmp_path):
        (tmp_path / "eval.yaml").write_text(
            "models: [{id: claude, provider: anthropic, max_retries: 0}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: [{id: c1, vars: {q: alpha}}]\n"
            'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n',
            encoding="utf-8",
        )
        return tmp_path

    def reflecting(self, monkeypatch, response):
        transport = httpx.MockTransport(lambda request: response(request))
        original = httpx.AsyncClient.__init__

        def patched(self, *args, **kwargs):
            kwargs["transport"] = transport
            original(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)

    def stored_text(self, tmp_path, run_id):
        return "".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in (tmp_path / "runs" / run_id).rglob("*")
            if path.is_file()
        )

    def test_a_non_json_body_is_redacted(self, tmp_path, monkeypatch):
        """The branch that was missed: the body isn't JSON, so it isn't parsed."""
        self.reflecting(
            monkeypatch,
            lambda request: httpx.Response(
                200, text=f"<html>gateway error, sent {request.headers['x-api-key']}</html>"
            ),
        )
        result = run_eval(load_config(self.project(tmp_path) / "eval.yaml"), settings_for(tmp_path))

        assert result.counts["failed"] == 1, "the call should have failed"
        assert "was not JSON" in (result.records[0].error or "")
        assert_no_secret(self.stored_text(tmp_path, result.run_id))
        assert "<redacted>" in self.stored_text(tmp_path, result.run_id)

    def test_a_json_error_body_is_redacted(self, tmp_path, monkeypatch):
        self.reflecting(
            monkeypatch,
            lambda request: httpx.Response(
                401, json={"error": {"message": f"bad key {request.headers['x-api-key']}"}}
            ),
        )
        result = run_eval(load_config(self.project(tmp_path) / "eval.yaml"), settings_for(tmp_path))
        assert_no_secret(self.stored_text(tmp_path, result.run_id))


class TestACommandScriptsOutputIsRedacted:
    """A script runs with evaling's environment, so its diagnostics hold the key."""

    def project(self, tmp_path, *, exit_code):
        (tmp_path / "leaky.py").write_text(
            "import os, sys\n"
            "sys.stderr.write('boom: ' + os.environ.get('MY_KEY_VAR', '') + '\\n')\n"
            f"sys.exit({exit_code})\n",
            encoding="utf-8",
        )
        (tmp_path / ".evaling.secrets.yaml").write_text(f"MY_KEY_VAR: {KEY}\n", encoding="utf-8")
        (tmp_path / "eval.yaml").write_text(
            "models:\n"
            "  - id: local\n"
            "    provider: command\n"
            "    command: python3 leaky.py\n"
            "    api_key_env: MY_KEY_VAR\n"
            "    max_retries: 0\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: [{id: c1, vars: {q: alpha}}]\n"
            'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n',
            encoding="utf-8",
        )
        return tmp_path

    def test_a_failing_scripts_stderr_is_redacted(self, tmp_path):
        path = self.project(tmp_path, exit_code=1)
        result = run_eval(load_config(path / "eval.yaml"), settings_for(path))

        error = result.records[0].error or ""
        assert "command exited 1" in error, error
        assert_no_secret(error)
        assert "<redacted>" in error

    def test_it_does_not_reach_the_run_directory(self, tmp_path):
        path = self.project(tmp_path, exit_code=1)
        result = run_eval(load_config(path / "eval.yaml"), settings_for(path))
        stored = "".join(
            p.read_text(encoding="utf-8", errors="ignore")
            for p in (path / "runs" / result.run_id).rglob("*")
            if p.is_file()
        )
        assert_no_secret(stored)

    def test_it_does_not_reach_an_export_or_report(self, tmp_path):
        path = self.project(tmp_path, exit_code=1)
        result = run_eval(load_config(path / "eval.yaml"), settings_for(path))
        store = RunStore(path / "runs")
        from evaling.export import export_run

        meta, records = store.load_meta(result.run_id), store.load_results(result.run_id)
        for fmt in ("json", "csv", "md", "html"):
            assert_no_secret(export_run(meta, records, fmt))


class TestTheResponseCacheIsScrubbedToo:
    """The cache is on by default, and it stores the completion before the record.

    Scrubbing only the record left the credential in `.evaling/cache/` on the
    normal path, while every test — which disables the cache — saw a clean
    run. Found by running the real binary, not by the suite.
    """

    def project(self, tmp_path):
        (tmp_path / "leaky.py").write_text(
            "import json, os, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'text': 'answer ' + os.environ.get('MY_KEY', '')}))\n",
            encoding="utf-8",
        )
        secrets = tmp_path / ".evaling.secrets.yaml"
        secrets.write_text(f"MY_KEY: {KEY}\n", encoding="utf-8")
        secrets.chmod(0o600)
        (tmp_path / "eval.yaml").write_text(
            "models:\n"
            "  - id: wrapped\n"
            "    provider: command\n"
            "    command: python3 leaky.py\n"
            "    api_key_env: MY_KEY\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: [{id: c1, vars: {q: a}}]\n"
            'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n',
            encoding="utf-8",
        )
        return tmp_path

    def everything_written(self, tmp_path):
        return "".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in tmp_path.rglob("*")
            if path.is_file() and path.name != ".evaling.secrets.yaml"
        )

    def test_nothing_on_disk_carries_it_with_the_cache_on(self, tmp_path):
        path = self.project(tmp_path)
        run_eval(load_config(path / "eval.yaml"), settings_for(path, cache=True))
        assert (path / "cache").is_dir(), "the cache was not exercised"
        assert_no_secret(self.everything_written(path))

    def test_a_cache_hit_still_serves_scrubbed_text(self, tmp_path):
        """The second run reads the cache rather than the script."""
        path = self.project(tmp_path)
        settings = settings_for(path, cache=True)
        run_eval(load_config(path / "eval.yaml"), settings)
        second = run_eval(load_config(path / "eval.yaml"), settings)

        assert second.counts["cached"] == 1, "the second run did not hit the cache"
        assert_no_secret(second.records[0].output or "")
        assert_no_secret(self.everything_written(path))
