"""No-look mode: the data must not survive the run in readable form.

The load-bearing test here is the canary. Everything else checks a mechanism;
the canary checks the promise — run the whole tool over data carrying a unique
marker, then search every artifact and every rendered surface for it. If it
appears anywhere, no-look is a lie regardless of how the mechanisms behave.
"""

import json

import pytest
from click.testing import CliRunner

from evaling.cli import main
from evaling.config import EvalConfig
from evaling.engine import run_eval
from evaling.export import export_run
from evaling.privacy import hash_case_id, redact_record
from evaling.storage import ResultRecord, RunStore
from helpers import make_settings

ENV = {"EVALING_USER_CONFIG": "/nonexistent", "EVALING_SECRETS": ""}
CANARY = "PATIENT-Zx9Q7-CANARY-8812"


def private_config(tmp_path, no_look=True, extra=None):
    """A config whose case data carries the canary in every field that flows."""
    data = {
        "models": [{"id": "mock", "provider": "mock"}],
        "variants": [
            {"name": "v1", "prompt": [{"role": "user", "content": "Summarize: {{ note }}"}]}
        ],
        "cases": [
            {
                "id": f"{CANARY}-case-{i}",
                "vars": {"note": f"{CANARY} record {i}"},
                "expected": CANARY,
            }
            for i in range(4)
        ],
        "scorecard": [{"criterion": "acc", "scorer": {"type": "contains"}}],
        "privacy": {"no_look": no_look},
    }
    if extra:
        data.update(extra)
    config = EvalConfig.model_validate(data)
    config._base_dir = tmp_path  # noqa: SLF001 - test fixture
    return config


class TestTheCanaryNeverEscapes:
    def test_no_artifact_or_rendering_contains_the_data(self, tmp_path):
        settings = make_settings(tmp_path, cache=True)
        result = run_eval(private_config(tmp_path), settings)
        assert result.counts["total"] == 4

        # 1. Nothing on disk anywhere under the output or cache directories.
        leaked = []
        for root in (settings.output_dir, settings.cache_dir, tmp_path):
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:  # pragma: no cover
                    continue
                if CANARY in text:
                    leaked.append(str(path))
        assert not leaked, f"case data written to disk: {leaked}"

        # 2. Nothing in what the run handed back in memory.
        for record in result.records:
            assert CANARY not in json.dumps(record.__dict__, default=str)
        assert CANARY not in json.dumps(result.aggregates)

        # 3. Nothing in any rendered view of the run.
        store = RunStore(settings.output_dir)
        meta = store.load_meta(result.run_id)
        records = store.load_results(result.run_id)
        for fmt in ("json", "csv", "md", "html"):
            assert CANARY not in export_run(meta, records, fmt), f"{fmt} export leaked"

        # 4. Nothing in any CLI output, including the verbose paths.
        runner = CliRunner()
        base = ["-o", str(settings.output_dir), "--cache-dir", str(settings.cache_dir)]
        for args in (
            ["show", result.run_id],
            ["show", result.run_id, "--failures"],
            ["-v", "show", result.run_id],
            ["export", result.run_id, "--format", "md"],
            ["export", result.run_id, "--format", "json"],
            ["list"],
        ):
            out = runner.invoke(main, base + args, env=ENV, catch_exceptions=False)
            assert CANARY not in out.output, f"CLI leaked via: {' '.join(args)}"

    def test_without_no_look_the_canary_is_present(self, tmp_path):
        """Proves the canary test can fail — otherwise it proves nothing."""
        settings = make_settings(tmp_path)
        result = run_eval(private_config(tmp_path, no_look=False), settings)
        stored = (settings.output_dir / result.run_id / "results.jsonl").read_text(encoding="utf-8")
        assert CANARY in stored


class TestWhatSurvives:
    def test_scores_and_metadata_are_intact(self, tmp_path):
        settings = make_settings(tmp_path)
        result = run_eval(private_config(tmp_path), settings)
        record = result.records[0]
        assert record.variant == "v1" and record.model == "mock"
        assert "acc" in record.scores
        assert result.aggregates["overall"]["cases"] == 4
        assert result.counts["succeeded"] == 4

    def test_payload_fields_are_emptied(self, tmp_path):
        result = run_eval(private_config(tmp_path), make_settings(tmp_path))
        for record in result.records:
            assert record.messages == []
            assert record.output is None

    def test_case_ids_are_hashed_by_default(self, tmp_path):
        """An id from production identifies a record as surely as the record."""
        result = run_eval(private_config(tmp_path), make_settings(tmp_path))
        for record in result.records:
            assert record.case_id.startswith("case-")
            assert CANARY not in record.case_id

    def test_raw_ids_require_an_explicit_opt_in(self, tmp_path):
        config = private_config(tmp_path)
        config = config.model_copy(
            update={"privacy": config.privacy.model_copy(update={"keep_case_ids": True})}
        )
        config._base_dir = tmp_path  # noqa: SLF001
        result = run_eval(config, make_settings(tmp_path))
        assert all(CANARY in record.case_id for record in result.records)

    def test_hashing_is_stable_across_runs(self):
        assert hash_case_id("abc") == hash_case_id("abc")
        assert hash_case_id("abc") != hash_case_id("abd")


class TestMechanisms:
    def test_the_response_cache_is_disabled(self, tmp_path):
        """The cache stores prompts and completions verbatim."""
        settings = make_settings(tmp_path, cache=True)
        run_eval(private_config(tmp_path), settings)
        cached = list(settings.cache_dir.rglob("*")) if settings.cache_dir.exists() else []
        assert [p for p in cached if p.is_file()] == []

    def test_inline_cases_are_stripped_from_the_config_snapshot(self, tmp_path):
        settings = make_settings(tmp_path)
        result = run_eval(private_config(tmp_path), settings)
        snapshot = (settings.output_dir / result.run_id / "config.snapshot.yaml").read_text(
            encoding="utf-8"
        )
        assert CANARY not in snapshot
        assert "redacted" in snapshot

    def test_errors_are_reduced_to_their_shape(self, tmp_path, monkeypatch):
        from evaling.providers import _REGISTRY
        from evaling.providers.mock import MockProvider

        class Exploding(MockProvider):
            async def complete(self, request):
                raise RuntimeError(f"upstream rejected: {CANARY}")

        monkeypatch.setitem(_REGISTRY, "mock", Exploding)
        result = run_eval(private_config(tmp_path), make_settings(tmp_path))
        for record in result.records:
            assert record.error is not None
            assert CANARY not in record.error
            assert "RuntimeError" in record.error  # still diagnostic

    def test_judge_rationales_are_dropped_but_other_details_kept(self, tmp_path):
        scorer = tmp_path / "scorer.py"
        scorer.write_text(
            "def score(output, case):\n"
            "    return {'score': 1.0, 'passed': True, 'detail': 'missing_field: postal_code'}\n",
            encoding="utf-8",
        )
        config = private_config(
            tmp_path,
            extra={
                "scorecard": [
                    {"criterion": "mine", "scorer": {"type": "python", "file": "scorer.py"}}
                ]
            },
        )
        result = run_eval(config, make_settings(tmp_path))
        # A Python scorer's detail is written by the user, who decides what is
        # safe to emit — so it survives.
        assert result.records[0].scores["mine"]["detail"] == "missing_field: postal_code"


class TestRedactRecordDirectly:
    def test_drops_judge_details_only(self):
        record = ResultRecord(variant="v", model="m", case_id="c")
        record.messages = [{"role": "user", "parts": [{"type": "text", "text": CANARY}]}]
        record.output = CANARY
        record.scores = {
            "judged": {"weight": 1.0, "score": 1.0, "passed": True, "detail": CANARY},
            "mine": {"weight": 1.0, "score": 1.0, "passed": True, "detail": "safe"},
        }
        redact_record(record, frozenset({"judged"}))
        assert record.messages == [] and record.output is None
        assert "detail" not in record.scores["judged"]
        assert record.scores["mine"]["detail"] == "safe"

    def test_is_idempotent(self):
        record = ResultRecord(variant="v", model="m", case_id="c", output=CANARY)
        redact_record(record)
        redact_record(record)
        assert record.output is None


class TestCliFlag:
    def config_file(self, tmp_path):
        path = tmp_path / "eval.yaml"
        path.write_text(
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ note }}"}]\n'
            f"cases: [{{id: c1, vars: {{note: {CANARY}}}}}]\n"
            "scorecard: [{criterion: acc, scorer: {type: contains, value: ''}}]\n",
            encoding="utf-8",
        )
        return path

    def test_flag_enables_no_look(self, tmp_path):
        out_dir = tmp_path / "runs"
        result = CliRunner().invoke(
            main,
            [
                "-o",
                str(out_dir),
                "--cache-dir",
                str(tmp_path / "c"),
                "run",
                "--no-look",
                str(self.config_file(tmp_path)),
            ],
            env=ENV,
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        stored = next(out_dir.rglob("results.jsonl")).read_text(encoding="utf-8")
        assert CANARY not in stored

    def test_flag_cannot_disable_config_privacy(self, tmp_path):
        """--no-look turns it on; there is deliberately no way to turn it off."""
        from evaling.cli import main as cli_main

        run_command = cli_main.commands["run"]
        flags = {param.name for param in run_command.params}
        assert "no_look" in flags
        assert "look" not in flags and "allow_payloads" not in flags


@pytest.mark.parametrize("no_look", [True, False])
def test_gate_and_thresholds_work_either_way(tmp_path, no_look):
    config = private_config(tmp_path, no_look=no_look, extra={"thresholds": {"min_pass_rate": 1.0}})
    result = run_eval(config, make_settings(tmp_path))
    assert result.gate is not None
    assert result.gate.passed is True
