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
#: A judge's canned reply lives in the config, so it is legitimately in the
#: config snapshot. It must never reach a *result*.
RATIONALE = "JUDGE-SAW-Kq4r7-2210"


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
    def failure_config(self, tmp_path):
        """Every path that can carry case content into an artifact.

        The original canary only ran cells that passed, so it never saw a
        failure detail — which is exactly where the leak was. This exercises a
        failing builtin scorer (reports the `expected` it wanted), a judge
        (rationale quotes what it graded), a scorer that raises (exception
        message holds the output), and a Python scorer (whose detail is
        allowed through, and must not carry anything it wasn't given).
        """
        (tmp_path / "rubric.yaml").write_text(
            "- role: user\n  content: 'Grade {{ output }}'\n", encoding="utf-8"
        )
        (tmp_path / "boom.py").write_text(
            "def score(output, case):\n    raise ValueError(f'scorer saw: {output}')\n",
            encoding="utf-8",
        )
        (tmp_path / "safe.py").write_text(
            "def score(output, case):\n"
            "    return {'score': 1.0, 'passed': True, 'detail': 'shape ok'}\n",
            encoding="utf-8",
        )
        config = EvalConfig.model_validate(
            {
                "models": [
                    {"id": "mock", "provider": "mock"},
                    {
                        "id": "judge-model",
                        "provider": "mock",
                        "role": "judge",
                        # A judge that succeeds and quotes what it graded, which
                        # is what makes a rationale dangerous.
                        "params": {
                            "response": (
                                '{"score": 1.0, "passed": true, '
                                f'"rationale": "the note read {RATIONALE}"}}'
                            )
                        },
                    },
                ],
                "variants": [
                    {"name": "v1", "prompt": [{"role": "user", "content": "Note: {{ note }}"}]}
                ],
                "cases": [
                    {"id": f"{CANARY}-{i}", "vars": {"note": f"{CANARY} {i}"}, "expected": CANARY}
                    for i in range(3)
                ],
                "judges": {"j": {"model": "judge-model", "rubric": "rubric.yaml"}},
                "scorecard": [
                    # fails, and its detail names the expected value it wanted
                    {"criterion": "exact-match", "scorer": {"type": "exact"}},
                    # the judge returns junk, so the scorer raises with the output in it
                    {"criterion": "judged", "scorer": {"type": "llm-judge", "judge": "j"}},
                    # raises, with the model output in the exception message
                    {"criterion": "raises", "scorer": {"type": "python", "file": "boom.py"}},
                    # allowed to keep its detail
                    {"criterion": "mine", "scorer": {"type": "python", "file": "safe.py"}},
                ],
                "privacy": {"no_look": True},
            }
        )
        config._base_dir = tmp_path  # noqa: SLF001
        return config

    def test_failure_paths_leak_nothing(self, tmp_path):
        """Failing scorers, a broken judge, and a raising scorer."""
        settings = make_settings(tmp_path, cache=True)
        result = run_eval(self.failure_config(tmp_path), settings, model_filter=["mock"])
        assert result.counts["total"] == 3

        # The run really did exercise the paths, not just avoid them.
        scores = result.records[0].scores
        assert scores["exact-match"]["passed"] is False  # detail names `expected`
        assert scores["judged"]["passed"] is True  # judge produced a rationale
        assert "error" in scores["raises"]  # scorer raised holding the output
        assert scores["mine"]["detail"] == "shape ok"  # whitelisted, survives

        # Case data must appear nowhere at all.
        leaked = []
        for root in (settings.output_dir, settings.cache_dir):
            for path in root.rglob("*"):
                if path.is_file() and CANARY in path.read_text(encoding="utf-8", errors="ignore"):
                    leaked.append(str(path))
        assert not leaked, f"case data written to disk: {leaked}"

        # The judge's rationale is in the config by construction, so it belongs
        # in the snapshot — but never in a result.
        results = (settings.output_dir / result.run_id / "results.jsonl").read_text(
            encoding="utf-8"
        )
        assert RATIONALE not in results, "judge rationale reached results.jsonl"
        assert CANARY not in results

        in_memory = json.dumps([r.__dict__ for r in result.records], default=str)
        assert CANARY not in in_memory and RATIONALE not in in_memory

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
    def test_keeps_only_whitelisted_details(self):
        """Only a user's own Python scorer decides what detail may leave."""
        record = ResultRecord(variant="v", model="m", case_id="c")
        record.messages = [{"role": "user", "parts": [{"type": "text", "text": CANARY}]}]
        record.output = CANARY
        record.scores = {
            "judged": {"weight": 1.0, "score": 1.0, "passed": True, "detail": CANARY},
            "builtin": {"weight": 1.0, "score": 0.0, "passed": False, "detail": CANARY},
            "mine": {"weight": 1.0, "score": 1.0, "passed": True, "detail": "safe"},
        }
        redact_record(record, frozenset({"mine"}))
        assert record.messages == [] and record.output is None
        assert "detail" not in record.scores["judged"]
        assert "detail" not in record.scores["builtin"]
        assert record.scores["mine"]["detail"] == "safe"

    def test_scorer_errors_are_replaced(self):
        record = ResultRecord(variant="v", model="m", case_id="c")
        record.scores = {"a": {"weight": 1.0, "score": 0.0, "passed": False, "error": CANARY}}
        redact_record(record)
        assert CANARY not in record.scores["a"]["error"]

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


class TestAttachmentsNeverReachDisk:
    """Media is the one payload with its own write path (`artifacts/`).

    The canary test above uses text-only cases, so it would not have caught an
    attachment being archived. This covers that path with real file bytes.
    """

    IMAGE_BYTES = b"CANARY-IMAGE-BYTES-9931"
    PDF_BYTES = b"CANARY-PDF-BYTES-4417"

    def media_config(self, tmp_path, no_look=True):
        data = tmp_path / "data"
        data.mkdir(exist_ok=True)
        (data / "secret.png").write_bytes(
            bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489")
            + self.IMAGE_BYTES
            + bytes.fromhex("0000000049454e44ae426082")
        )
        (data / "secret.pdf").write_bytes(b"%PDF-1.4\n" + self.PDF_BYTES + b"\n%%EOF\n")
        config = EvalConfig.model_validate(
            {
                "models": [{"id": "mock", "provider": "mock"}],
                "variants": [
                    {
                        "name": "describe",
                        "prompt": [
                            {
                                "role": "user",
                                "content": [
                                    {"text": "Describe: {{ note }}"},
                                    {"image": "{{ files.photo }}"},
                                    {"file": "{{ files.doc }}"},
                                ],
                            }
                        ],
                    }
                ],
                "cases": [
                    {
                        "id": f"{CANARY}-media",
                        "vars": {"note": CANARY},
                        "files": {"photo": "data/secret.png", "doc": "data/secret.pdf"},
                    }
                ],
                "scorecard": [{"criterion": "ok", "scorer": {"type": "contains", "value": ""}}],
                "privacy": {"no_look": no_look},
            }
        )
        config._base_dir = tmp_path  # noqa: SLF001 - test fixture
        return config

    def test_artifacts_directory_stays_empty(self, tmp_path):
        settings = make_settings(tmp_path, cache=True)
        result = run_eval(self.media_config(tmp_path), settings)
        assert result.counts["succeeded"] == 1
        artifacts = settings.output_dir / result.run_id / "artifacts"
        assert list(artifacts.iterdir()) == []

    def test_no_file_bytes_or_paths_are_written(self, tmp_path):
        settings = make_settings(tmp_path, cache=True)
        run_eval(self.media_config(tmp_path), settings)

        markers = [self.IMAGE_BYTES, self.PDF_BYTES, b"secret.png", b"secret.pdf", CANARY.encode()]
        leaks = []
        roots = [settings.output_dir] + (
            [settings.cache_dir] if settings.cache_dir.exists() else []
        )
        for root in roots:
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                blob = path.read_bytes()
                leaks += [(str(path), m) for m in markers if m in blob]
        assert not leaks, f"attachment data written to disk: {leaks}"

    def test_without_no_look_the_attachment_is_archived(self, tmp_path):
        """Proves the assertions above are testing something."""
        settings = make_settings(tmp_path)
        result = run_eval(self.media_config(tmp_path, no_look=False), settings)
        artifacts = settings.output_dir / result.run_id / "artifacts"
        stored = list(artifacts.iterdir())
        assert stored, "media should normally be content-addressed into artifacts/"
        assert any(self.IMAGE_BYTES in p.read_bytes() for p in stored)
