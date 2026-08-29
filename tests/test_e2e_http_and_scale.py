"""End-to-end coverage the mock-provider fixtures don't reach.

- a full run through a real HTTP provider (faked transport, no network)
- a run large enough to shake out per-cell overhead and memory assumptions
- resume after an actual killed process, not a simulated one
"""

import json
import os
import subprocess
import sys
import textwrap

import httpx
import pytest

from evaling.config import load_config
from evaling.engine import run_eval
from evaling.storage import RunStore
from helpers import make_config, make_settings

HTTP_CONFIG = """\
models:
  - id: gpt-5.2
    provider: openai
    params: {pricing: {input: 1.0, output: 2.0}}
variants:
  - name: v1
    prompt: [{role: user, content: "{{ q }}"}]
cases:
  - {id: good, vars: {q: alpha}, expected: ALPHA}
  - {id: bad, vars: {q: beta}, expected: NOPE}
scorecard: [{criterion: acc, scorer: {type: exact}}]
thresholds: {min_pass_rate: 0.9}
"""


class TestHttpProviderEndToEnd:
    """The whole pipeline over a real provider class, transport faked."""

    def test_full_run_through_openai_provider(self, tmp_path, monkeypatch):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            asked = seen[-1]["messages"][-1]["content"]
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": asked.upper()}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 4},
                },
            )

        # Patch the transport at the provider level, so everything above it —
        # engine, scorers, storage, gating — runs for real.
        from evaling.providers import http as http_module

        def fake_client(self):
            if self._client is None:
                self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            return self._client

        monkeypatch.setattr(http_module.HttpProvider, "client", fake_client)

        (tmp_path / "eval.yaml").write_text(HTTP_CONFIG, encoding="utf-8")
        (tmp_path / ".evaling.secrets.yaml").write_text(
            "OPENAI_API_KEY: sk-test\n", encoding="utf-8"
        )
        (tmp_path / ".evaling.secrets.yaml").chmod(0o600)

        config = load_config(tmp_path / "eval.yaml")
        settings = make_settings(tmp_path)
        result = run_eval(config, settings)

        # `seen` is only appended to by the fake, so two entries is the proof
        # that the patch took — comparing the two objects was not, since a
        # bound method and a test function are never the same object.
        assert len(seen) == 2
        assert seen[0]["model"] == "gpt-5.2"

        by_case = {record.case_id: record for record in result.records}
        assert by_case["good"].output == "ALPHA"
        assert by_case["good"].scores["acc"]["passed"] is True
        assert by_case["bad"].scores["acc"]["passed"] is False
        # usage and configured pricing flowed through
        assert by_case["good"].input_tokens == 10
        assert result.totals["cost_usd"] == pytest.approx(2 * (10 * 1e-6 + 4 * 2e-6))
        # gate evaluated against the real aggregates
        assert result.gate is not None and not result.gate.passed

        # and the run is readable back from disk
        store = RunStore(settings.output_dir)
        assert len(store.load_results(result.run_id)) == 2

    def test_http_error_becomes_a_cell_error_not_a_crash(self, tmp_path, monkeypatch):
        from evaling.providers import http as http_module

        def fake_client(self):
            if self._client is None:
                self._client = httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(
                            400, json={"error": {"message": "bad request"}}
                        )
                    )
                )
            return self._client

        monkeypatch.setattr(http_module.HttpProvider, "client", fake_client)
        (tmp_path / "eval.yaml").write_text(HTTP_CONFIG, encoding="utf-8")
        (tmp_path / ".evaling.secrets.yaml").write_text(
            "OPENAI_API_KEY: sk-test\n", encoding="utf-8"
        )
        (tmp_path / ".evaling.secrets.yaml").chmod(0o600)

        result = run_eval(load_config(tmp_path / "eval.yaml"), make_settings(tmp_path))
        assert result.counts["failed"] == 2
        assert all("HTTP 400" in record.error for record in result.records)


class TestScale:
    @pytest.mark.slow
    def test_five_hundred_cells(self, tmp_path):
        """A run an order of magnitude past the fixtures, end to end."""
        cases = [{"id": f"c{i}", "vars": {"q": f"q{i}"}, "expected": f"q{i}"} for i in range(500)]
        config = make_config(tmp_path, cases=cases)
        settings = make_settings(tmp_path, concurrency=16)
        result = run_eval(config, settings)

        assert result.counts == {
            "total": 500,
            "succeeded": 500,
            "failed": 0,
            "cached": 0,
        }
        assert result.aggregates["overall"]["pass_rate"] == 1.0

        # every cell landed on disk exactly once
        store = RunStore(settings.output_dir)
        keys = [record.key for record in store.iter_results(result.run_id)]
        assert len(keys) == 500 and len(set(keys)) == 500


class TestResumeAfterRealKill:
    @pytest.mark.slow
    def test_killed_process_resumes(self, tmp_path):
        """Kill a real run mid-flight, then resume it — no simulation."""
        runner = tmp_path / "runner.py"
        runner.write_text(
            textwrap.dedent(
                """
                import sys, time
                from evaling.config import load_config, Settings
                from evaling.engine import run_eval

                config = load_config(sys.argv[1])
                settings = Settings.model_validate(
                    {"output_dir": sys.argv[2], "cache_dir": sys.argv[3],
                     "cache": False, "concurrency": 1}
                )

                def on_result(record):
                    print(record.case_id, flush=True)
                    time.sleep(0.4)  # leave a window to be killed in

                run_eval(config, settings, on_result=on_result)
                """
            )
        )
        (tmp_path / "eval.yaml").write_text(
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases:\n"
            + "".join(f"  - {{id: c{i}, vars: {{q: '{i}'}}}}\n" for i in range(6))
            + "scorecard: [{criterion: acc, scorer: {type: contains, value: ''}}]\n"
        )
        out_dir = tmp_path / "runs"
        env = dict(os.environ, PYTHONPATH=str(tmp_path))

        process = subprocess.Popen(
            [
                sys.executable,
                str(runner),
                str(tmp_path / "eval.yaml"),
                str(out_dir),
                str(tmp_path / "cache"),
            ],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        # wait until at least two cells are done, then SIGKILL mid-run
        done = 0
        for _ in range(2):
            line = process.stdout.readline()
            assert line, "runner produced no output"
            done += 1
        process.kill()
        process.wait(timeout=10)
        assert done == 2

        store = RunStore(out_dir)
        [meta] = store.list_runs()
        assert meta["status"] == "running"  # never finalized
        partial = len(store.load_results(meta["id"]))
        assert 0 < partial < 6

        # resume with the same config and finish the job
        resumed = run_eval(
            load_config(tmp_path / "eval.yaml"),
            make_settings(tmp_path).model_copy(update={"output_dir": out_dir}),
            resume_run_id=meta["id"],
        )
        assert resumed.run_id == meta["id"]
        assert resumed.counts["total"] == 6
        keys = [record.key for record in store.iter_results(meta["id"])]
        assert len(set(keys)) == 6, "resume must not duplicate or drop cells"
