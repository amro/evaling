"""`--log-requests`: a JSONL trace of what evaling sent and got back.

For debugging a provider that misbehaves without adding print statements to
evaling and running it from a checkout. The two properties that make it safe
to turn on: it never writes headers (where the API key lives), and it is
refused under no-look, where a verbatim record of prompts and completions is
the exact artifact the mode exists to prevent.
"""

import json

import httpx
import pytest
from click.testing import CliRunner

from evaling.cli import main
from evaling.config import Settings, load_config
from evaling.engine import run_eval
from evaling.errors import EvalingError
from evaling.reqlog import TRACE_MARKER, TRACE_VERSION, RequestLog, open_log

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}

CONFIG = """\
models: [{id: mock, provider: mock}]
variants:
  - name: v1
    prompt: [{role: user, content: "{{ q }}"}]
cases: [{id: c1, vars: {q: alpha}}, {id: c2, vars: {q: beta}}]
scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "eval.yaml").write_text(CONFIG, encoding="utf-8")
    return tmp_path


def settings_for(path):
    return Settings.model_validate(
        {
            "output_dir": str(path / "runs"),
            "cache_dir": str(path / "cache"),
            "cache": False,
            "concurrency": 2,
        }
    )


def entries(path):
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


class TestWhatItWrites:
    def test_one_entry_per_call(self, project):
        log = project / "trace.jsonl"
        run_eval(load_config(project / "eval.yaml"), settings_for(project), log_requests=log)

        written = entries(log)
        assert len(written) == 2
        assert {entry["model"] for entry in written} == {"mock"}
        assert all("request" in entry and "response" in entry for entry in written)

    def test_the_prompt_that_was_sent_is_in_it(self, project):
        log = project / "trace.jsonl"
        run_eval(load_config(project / "eval.yaml"), settings_for(project), log_requests=log)
        texts = [entry["request"]["messages"][0]["text"] for entry in entries(log)]
        assert sorted(texts) == ["alpha", "beta"]

    def test_it_is_valid_jsonl(self, project):
        """Written to be grepped and piped to jq, so every line must parse."""
        log = project / "trace.jsonl"
        run_eval(load_config(project / "eval.yaml"), settings_for(project), log_requests=log)
        for line in log.read_text(encoding="utf-8").splitlines():
            json.loads(line)

    def test_each_run_starts_a_fresh_log(self, project):
        """A log that accumulates across runs is unreadable, and the question
        is always about the run you just made."""
        log = project / "trace.jsonl"
        for _ in range(3):
            run_eval(load_config(project / "eval.yaml"), settings_for(project), log_requests=log)
        assert len(entries(log)) == 2


class TestItNeverLeaksCredentials:
    def test_headers_are_not_recorded(self, project, tmp_path, monkeypatch):
        """The key lives in a header, so no code path may write headers."""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("x-api-key")
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "hi"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient.__init__

        def patched(self, *args, **kwargs):
            kwargs["transport"] = transport
            original(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
        (tmp_path / "eval.yaml").write_text(
            "models: [{id: claude, provider: anthropic, params: {max_tokens: 8}}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: [{id: c1, vars: {q: alpha}}]\n"
            'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-the-actual-secret")
        log = tmp_path / "trace.jsonl"
        run_eval(load_config(tmp_path / "eval.yaml"), settings_for(tmp_path), log_requests=log)

        text = log.read_text(encoding="utf-8")
        assert seen["auth"] == "sk-ant-the-actual-secret", "the key was not actually sent"
        assert "sk-ant-the-actual-secret" not in text
        assert "x-api-key" not in text.lower()
        # ...and the useful half is still there.
        assert "max_tokens" in text and "200" in text

    def test_an_environment_key_reflected_in_a_response_is_redacted(self, tmp_path, monkeypatch):
        """The case the first version of this got wrong.

        Redaction covered secrets-*file* values only, so a key from the real
        environment — the normal case — was not scrubbed. A gateway that
        echoes the request header into its error body then wrote a live
        credential to disk, while the stored run record redacted it correctly.
        """
        key = "sk-ant-live-key-from-the-environment"

        def handler(request: httpx.Request) -> httpx.Response:
            # A misconfigured gateway reflecting what it was sent.
            return httpx.Response(
                500, json={"error": {"message": f"bad auth header: {request.headers['x-api-key']}"}}
            )

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient.__init__

        def patched(self, *args, **kwargs):
            kwargs["transport"] = transport
            original(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
        (tmp_path / "eval.yaml").write_text(
            "models: [{id: claude, provider: anthropic, max_retries: 0}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: [{id: c1, vars: {q: alpha}}]\n"
            'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n',
            encoding="utf-8",
        )
        log = tmp_path / "trace.jsonl"
        run_eval(load_config(tmp_path / "eval.yaml"), settings_for(tmp_path), log_requests=log)

        text = log.read_text(encoding="utf-8")
        assert "bad auth header" in text, "the reflected body was not logged at all"
        assert key not in text
        assert "<redacted>" in text

    def test_a_secret_reflected_in_a_response_is_redacted(self, tmp_path):
        """A gateway that echoes credentials into its body must not persist one."""

        class FakeEnv(dict):
            secret_values = ["sk-super-secret-value"]

        log = open_log(tmp_path / "trace.jsonl", FakeEnv(), no_look=False)
        log.record(model="m", response={"error": "bad key sk-super-secret-value"})
        text = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
        assert "sk-super-secret-value" not in text
        assert "<redacted>" in text


class TestNoLookRefusesIt:
    def test_the_two_cannot_be_asked_for_together(self, project):
        (project / "eval.yaml").write_text(CONFIG + "privacy: {no_look: true}\n", encoding="utf-8")
        with pytest.raises(EvalingError, match="cannot be written in no-look mode"):
            run_eval(
                load_config(project / "eval.yaml"),
                settings_for(project),
                log_requests=project / "trace.jsonl",
            )

    def test_nothing_is_written_before_the_refusal(self, project):
        (project / "eval.yaml").write_text(CONFIG + "privacy: {no_look: true}\n", encoding="utf-8")
        log = project / "trace.jsonl"
        with pytest.raises(EvalingError):
            run_eval(load_config(project / "eval.yaml"), settings_for(project), log_requests=log)
        assert not log.exists()

    def test_the_cli_flag_is_refused_too(self, project):
        (project / "eval.yaml").write_text(CONFIG + "privacy: {no_look: true}\n", encoding="utf-8")
        result = CliRunner().invoke(
            main,
            [
                "-c",
                str(project / "eval.yaml"),
                "-o",
                str(project / "runs"),
                "run",
                "--log-requests",
                str(project / "trace.jsonl"),
            ],
            env=ENV,
            catch_exceptions=False,
        )
        assert result.exit_code == 2
        assert "no-look" in result.output

    def test_the_no_look_flag_counts_too(self, project):
        """--no-look turns the mode on, so it must close this door as well."""
        result = CliRunner().invoke(
            main,
            [
                "-c",
                str(project / "eval.yaml"),
                "-o",
                str(project / "runs"),
                "run",
                "--no-look",
                "--log-requests",
                str(project / "trace.jsonl"),
            ],
            env=ENV,
            catch_exceptions=False,
        )
        assert result.exit_code == 2
        assert "no-look" in result.output


class TestItNeverBreaksARun:
    def test_an_unwritable_log_fails_before_anything_is_spent(self, project):
        with pytest.raises(EvalingError, match="could not open request log"):
            run_eval(
                load_config(project / "eval.yaml"),
                settings_for(project),
                log_requests=project / "missing-dir" / "x" / "\x00bad",
            )

    def test_an_empty_path_is_refused(self, project):
        with pytest.raises(EvalingError, match="empty path"):
            run_eval(load_config(project / "eval.yaml"), settings_for(project), log_requests="")

    def test_a_write_failure_mid_run_does_not_fail_the_run(self, project, monkeypatch):
        """The run being diagnosed matters more than the diagnosis of it."""
        log = RequestLog(project / "trace.jsonl")
        log.record(model="before")
        assert entries(project / "trace.jsonl"), "the log was never working, so nothing broke"

        exploded = []

        def explode(*args, **kwargs):
            exploded.append(True)
            raise OSError("disk full")

        monkeypatch.setattr(type(log.path), "open", explode, raising=False)
        log.record(model="m")  # must not raise
        # Without this the test passes when the patch misses — swapping
        # `self.path.open(...)` for `open(self.path, ...)` would leave the
        # write working and the failure never injected.
        assert exploded, "the write failure was never injected"

        monkeypatch.undo()
        log.record(model="after")
        written = [entry["model"] for entry in entries(project / "trace.jsonl")]
        assert written == ["before", "after"], "the log stopped working after one failure"

    def test_an_unusual_value_is_rendered_rather_than_dropped(self, project):
        """`default=str` is what keeps a Path or a datetime in the trace.

        Without it the whole entry raises and falls back to a placeholder
        line, losing the call it was recording. Asserting only that *a* line
        was written could not tell the two apart — mutation testing could.
        """
        from pathlib import Path

        where = Path("/tmp/somewhere")
        log = RequestLog(project / "trace.jsonl")
        log.record(model="m", where=where, count=3)
        [written] = entries(project / "trace.jsonl")
        # str(Path(...)), not the literal: separators differ on Windows.
        assert written["where"] == str(where)
        assert written["count"] == 3
        assert "error" not in written, "the entry fell back instead of serializing"


class TestThroughTheCli:
    def test_the_flag_writes_the_file_and_says_so(self, project):
        log = project / "trace.jsonl"
        result = CliRunner().invoke(
            main,
            [
                "-c",
                str(project / "eval.yaml"),
                "-o",
                str(project / "runs"),
                "run",
                "--log-requests",
                str(log),
            ],
            env=ENV,
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "request log written to" in result.output
        assert len(entries(log)) == 2

    def test_an_empty_path_is_a_usage_error(self, project):
        result = CliRunner().invoke(
            main,
            [
                "-c",
                str(project / "eval.yaml"),
                "-o",
                str(project / "runs"),
                "run",
                "--log-requests",
                "",
            ],
            env=ENV,
            catch_exceptions=False,
        )
        assert result.exit_code == 2
        assert "empty path" in result.output


class TestTheThingsThatMustNotBreakARun:
    """A debugging flag gets to fail at nothing.

    Mutation testing found these unguarded: the serialization fallback, the
    non-UTF-8 file check, the one-byte file, and the nested parent directory.
    """

    def test_a_circular_entry_falls_back_rather_than_raising(self, project):
        """`default=str` handles almost everything — not a cycle.

        The fallback line was marked unreachable and was not: json.dumps
        raises ValueError on a circular reference whatever `default` says.
        """
        circular = {}
        circular["self"] = circular
        log = RequestLog(project / "trace.jsonl")
        log.record(model="m", payload=circular)
        [written] = entries(project / "trace.jsonl")
        assert written == {
            "error": "entry was not serializable",
            TRACE_MARKER: TRACE_VERSION,
        }

    def test_a_nested_parent_directory_is_created(self, project):
        log = RequestLog(project / "deep" / "nested" / "trace.jsonl")
        log.record(model="m")
        assert len(entries(project / "deep" / "nested" / "trace.jsonl")) == 1


class TestRefusingSomebodyElsesFile:
    @pytest.mark.parametrize(
        "content",
        [
            '{"id":"case-1","vars":{"q":"keep me"}}\n',
            '{"type":"object"}\n',
            '{"case_id":"c1","output":"keep me"}\n',
            "[]\n",
            "null\n",
            "42\n",
            "\n",
            '\n{"id":"case-1"}\n',
            '{"model":"m","request":{},"response":{}}\n',
            '{"_evaling_request_log":true}\n',
            '{"_evaling_request_log":2}\n',
            '{"_evaling_request_log":1}\n{"id":"case-1"}\n',
        ],
    )
    def test_only_marked_traces_can_be_overwritten(self, project, content):
        target = project / "valuable.jsonl"
        target.write_text(content, encoding="utf-8")
        before = target.read_bytes()
        with pytest.raises(EvalingError, match="refusing to overwrite"):
            RequestLog(target)
        assert target.read_bytes() == before

    def test_cli_refuses_its_own_dataset_as_the_log(self, project):
        target = project / "cases.jsonl"
        target.write_text('{"id":"c1","vars":{"q":"keep me"}}\n', encoding="utf-8")
        config = CONFIG.replace(
            "cases: [{id: c1, vars: {q: alpha}}, {id: c2, vars: {q: beta}}]",
            "cases: {file: cases.jsonl}",
        )
        (project / "eval.yaml").write_text(config, encoding="utf-8")
        before = target.read_bytes()
        result = CliRunner().invoke(
            main,
            ["run", str(project / "eval.yaml"), "--log-requests", str(target)],
            env=ENV,
        )
        assert result.exit_code == 2
        assert "refusing to overwrite" in result.output
        assert target.read_bytes() == before

    def test_a_file_that_is_not_utf8_is_refused_not_raised(self, project):
        """The first line is read with errors="replace" for exactly this.

        A user pointing --log-requests at a binary file should be told no,
        not shown a UnicodeDecodeError from inside evaling.
        """
        target = project / "photo.jpg"
        target.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" * 4)
        with pytest.raises(EvalingError, match="refusing to overwrite"):
            RequestLog(target)
        assert target.read_bytes().startswith(b"\xff\xd8"), "the file was touched"

    def test_a_one_byte_file_is_still_somebody_elses(self, project):
        """The size check is for *empty*, and one byte is not empty."""
        target = project / "notes.txt"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(EvalingError, match="refusing to overwrite"):
            RequestLog(target)
        assert target.read_text(encoding="utf-8") == "x"

    def test_an_empty_file_is_fair_game(self, project):
        target = project / "empty.jsonl"
        target.write_text("", encoding="utf-8")
        RequestLog(target).record(model="m")
        assert len(entries(target)) == 1


class TestWhatCountsAsASecretWorthScrubbing:
    """Too short a value would mangle ordinary text everywhere it appeared."""

    def test_a_short_value_is_not_registered(self, project):
        log = RequestLog(project / "trace.jsonl")
        log.add_secret("abc")
        log.record(model="m", note="abc appears in ordinary words like abcdef")
        [written] = entries(project / "trace.jsonl")
        assert "abc appears" in written["note"], "a 3-character 'secret' was redacted"

    def test_a_value_at_the_minimum_length_is_registered(self, project):
        value = "x" * RequestLog.MIN_SECRET
        log = RequestLog(project / "trace.jsonl")
        log.add_secret(value)
        log.record(model="m", note=f"key is {value}")
        [written] = entries(project / "trace.jsonl")
        assert value not in written["note"]
        assert "<redacted>" in written["note"]

    def test_one_below_the_minimum_is_not(self):
        log = RequestLog.__new__(RequestLog)
        log._secrets = []
        log.add_secret("y" * (RequestLog.MIN_SECRET - 1))
        assert log._secrets == []

    def test_the_same_secret_is_not_registered_twice(self):
        log = RequestLog.__new__(RequestLog)
        log._secrets = []
        for _ in range(3):
            log.add_secret("sk-ant-duplicate-value")
        assert log._secrets == ["sk-ant-duplicate-value"]

    def test_nothing_is_registered_for_an_empty_value(self):
        log = RequestLog.__new__(RequestLog)
        log._secrets = []
        log.add_secret(None)
        log.add_secret("")
        assert log._secrets == []
