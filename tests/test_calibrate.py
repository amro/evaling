"""`evaling calibrate`: turn the meta-eval recipe into a command.

docs/evaluating-judges.md describes a config shape; this generates it. It is
the step between "I have a judge" and "I trust this judge to gate CI", and the
one people skip, so the generated project has to be runnable as written and
honest about what it left out.

It generates only: no model is called, so none of this touches a network.
"""

import json

import pytest
import yaml
from click.testing import CliRunner

from evaling.calibrate import CalibrationError, build_cases, load_labels
from evaling.cli import main
from evaling.config import EvalConfig, Settings, load_config
from evaling.engine import run_eval
from evaling.storage import RunStore

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}

CONFIG = """\
models: [{id: mock, provider: mock}]
variants:
  - name: v1
    prompt: [{role: user, content: "{{ q }}"}]
cases:
  - {id: a1, vars: {q: alpha}}
  - {id: a2, vars: {q: beta}}
  - {id: a3, vars: {q: gamma}}
scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]
"""


@pytest.fixture
def rated(tmp_path):
    """A finished run, plus ratings for its outputs."""
    (tmp_path / "eval.yaml").write_text(CONFIG, encoding="utf-8")
    settings = Settings.model_validate(
        {
            "output_dir": str(tmp_path / "runs"),
            "cache_dir": str(tmp_path / "cache"),
            "cache": False,
        }
    )
    result = run_eval(load_config(tmp_path / "eval.yaml"), settings)
    (tmp_path / "labels.csv").write_text(
        "case_id,human_label\na1,5\na2,3\na3,1\n", encoding="utf-8"
    )
    return tmp_path, result


def invoke(path, *args):
    return CliRunner().invoke(
        main,
        ["-c", str(path / "eval.yaml"), "-o", str(path / "runs"), *args],
        env=ENV,
        catch_exceptions=False,
    )


class TestReadingLabels:
    def test_csv(self, tmp_path):
        (tmp_path / "l.csv").write_text("case_id,human_label\nx,4\ny,2\n", encoding="utf-8")
        assert load_labels(tmp_path / "l.csv") == {"x": 4, "y": 2}

    def test_jsonl(self, tmp_path):
        (tmp_path / "l.jsonl").write_text(
            '{"case_id": "x", "human_label": 4}\n{"case_id": "y", "human_label": 2}\n',
            encoding="utf-8",
        )
        assert load_labels(tmp_path / "l.jsonl") == {"x": 4, "y": 2}

    @pytest.mark.parametrize("column", ["human_label", "label", "rating", "score"])
    def test_the_common_column_spellings_all_work(self, tmp_path, column):
        (tmp_path / "l.csv").write_text(f"id,{column}\nx,4\n", encoding="utf-8")
        assert load_labels(tmp_path / "l.csv") == {"x": 4}

    def test_ratings_arrive_as_numbers(self, tmp_path):
        """CSV gives strings, and the agreement scorer compares numbers."""
        (tmp_path / "l.csv").write_text("id,rating\nx,4\ny,3.5\n", encoding="utf-8")
        labels = load_labels(tmp_path / "l.csv")
        assert labels == {"x": 4, "y": 3.5}
        assert isinstance(labels["x"], int) and isinstance(labels["y"], float)

    def test_a_non_numeric_rating_survives_as_written(self, tmp_path):
        """Not every rating is a number — "good"/"bad" is a legitimate scale."""
        (tmp_path / "l.csv").write_text("id,rating\nx,good\n", encoding="utf-8")
        assert load_labels(tmp_path / "l.csv") == {"x": "good"}

    def test_a_missing_id_column_says_what_is_needed(self, tmp_path):
        (tmp_path / "l.csv").write_text("something,human_label\nx,4\n", encoding="utf-8")
        with pytest.raises(CalibrationError, match="case_id"):
            load_labels(tmp_path / "l.csv")

    def test_a_missing_rating_names_the_accepted_columns(self, tmp_path):
        (tmp_path / "l.csv").write_text("case_id,notes\nx,hello\n", encoding="utf-8")
        with pytest.raises(CalibrationError, match="human_label"):
            load_labels(tmp_path / "l.csv")

    def test_an_empty_file(self, tmp_path):
        (tmp_path / "l.jsonl").write_text("", encoding="utf-8")
        with pytest.raises(CalibrationError, match="no rows"):
            load_labels(tmp_path / "l.jsonl")

    def test_a_missing_file(self, tmp_path):
        with pytest.raises(CalibrationError, match="not found"):
            load_labels(tmp_path / "nope.csv")


class TestPairingOutputsWithRatings:
    def test_each_rated_case_becomes_one_row(self, rated):
        path, result = rated
        records = RunStore(path / "runs").load_results(result.run_id)
        cases = build_cases(records, {"a1": 5, "a2": 3, "a3": 1})
        assert [case["id"] for case in cases] == ["a1", "a2", "a3"]
        assert all(case["answer"] for case in cases)
        assert [case["human_label"] for case in cases] == [5, 3, 1]

    def test_unrated_cases_are_left_out(self, rated):
        path, result = rated
        records = RunStore(path / "runs").load_results(result.run_id)
        cases = build_cases(records, {"a1": 5})
        assert [case["id"] for case in cases] == ["a1"]

    def test_a_run_with_several_variants_needs_one_named(self, tmp_path):
        """A judge grades an answer; three variants means three answers."""
        (tmp_path / "eval.yaml").write_text(
            CONFIG.replace(
                '  - name: v1\n    prompt: [{role: user, content: "{{ q }}"}]\n',
                '  - name: v1\n    prompt: [{role: user, content: "{{ q }}"}]\n'
                '  - name: v2\n    prompt: [{role: user, content: "say {{ q }}"}]\n',
            ),
            encoding="utf-8",
        )
        settings = Settings.model_validate({"output_dir": str(tmp_path / "runs"), "cache": False})
        result = run_eval(load_config(tmp_path / "eval.yaml"), settings)
        records = RunStore(tmp_path / "runs").load_results(result.run_id)

        with pytest.raises(CalibrationError, match="--variant"):
            build_cases(records, {"a1": 5})
        chosen = build_cases(records, {"a1": 5}, variant="v2")
        assert chosen[0]["answer"] == "say alpha"

    def test_a_run_with_several_models_needs_one_named(self, tmp_path):
        """Three models produced three answers; the rating refers to one.

        Taking whichever was written first builds a calibration set measuring
        agreement against outputs the rater may never have seen — plausible
        numbers about the wrong thing.
        """
        (tmp_path / "eval.yaml").write_text(
            CONFIG.replace(
                "models: [{id: mock, provider: mock}]",
                "models: [{id: m1, provider: mock},"
                " {id: m2, provider: mock, params: {response: different}}]",
            ),
            encoding="utf-8",
        )
        settings = Settings.model_validate({"output_dir": str(tmp_path / "runs"), "cache": False})
        result = run_eval(load_config(tmp_path / "eval.yaml"), settings)
        records = RunStore(tmp_path / "runs").load_results(result.run_id)

        with pytest.raises(CalibrationError, match="--model"):
            build_cases(records, {"a1": 5})
        chosen = build_cases(records, {"a1": 5}, model="m2")
        assert chosen[0]["answer"] == "different"

    @pytest.mark.parametrize("kind", ["variant", "model"])
    def test_a_filter_that_matches_nothing_names_the_typo(self, rated, kind):
        """Not "no case matches a labelled id", which blames the ratings."""
        path, result = rated
        records = RunStore(path / "runs").load_results(result.run_id)
        with pytest.raises(CalibrationError, match=f"no {kind} 'nope'"):
            build_cases(records, {"a1": 5}, **{kind: "nope"})

    def test_labels_that_match_nothing_say_so(self, rated):
        path, result = rated
        records = RunStore(path / "runs").load_results(result.run_id)
        with pytest.raises(CalibrationError, match="no case in the run matches"):
            build_cases(records, {"someone-elses-id": 5})


class TestTheGeneratedProject:
    def test_it_writes_a_runnable_config(self, rated):
        path, result = rated
        out = path / "calib"
        assert (
            invoke(
                path,
                "calibrate",
                "--from-run",
                "latest",
                "--labels",
                str(path / "labels.csv"),
                "--out",
                str(out),
            ).exit_code
            == 0
        )

        config = yaml.safe_load((out / "eval.yaml").read_text(encoding="utf-8"))
        EvalConfig.model_validate(config)  # the schema is the arbiter

    def test_it_validates_end_to_end(self, rated):
        """Generated and then immediately checked the way a reader would."""
        path, _ = rated
        out = path / "calib"
        invoke(
            path,
            "calibrate",
            "--from-run",
            "latest",
            "--labels",
            str(path / "labels.csv"),
            "--out",
            str(out),
        )

        result = CliRunner().invoke(
            main,
            ["-c", str(out / "eval.yaml"), "validate"],
            env={**ENV, "ANTHROPIC_API_KEY": "not-used-by-validate"},
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        # 3 cases x 2 rubrics
        assert "6 requests" in result.output

    def test_the_cases_carry_the_answers_and_ratings(self, rated):
        path, _ = rated
        out = path / "calib"
        invoke(
            path,
            "calibrate",
            "--from-run",
            "latest",
            "--labels",
            str(path / "labels.csv"),
            "--out",
            str(out),
        )

        rows = [
            json.loads(line)
            for line in (out / "calibration.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert {row["id"] for row in rows} == {"a1", "a2", "a3"}
        assert all(row["answer"] and row["human_label"] is not None for row in rows)

    def test_the_rubrics_differ_from_each_other(self, rated):
        """One rubric has nothing to be better than."""
        path, _ = rated
        out = path / "calib"
        invoke(
            path,
            "calibrate",
            "--from-run",
            "latest",
            "--labels",
            str(path / "labels.csv"),
            "--out",
            str(out),
        )
        strict = (out / "rubrics" / "strict.yaml").read_text(encoding="utf-8")
        lenient = (out / "rubrics" / "lenient.yaml").read_text(encoding="utf-8")
        assert strict != lenient
        assert "{{ answer }}" in strict and "{{ answer }}" in lenient

    def test_it_scores_agreement_against_the_human_label(self, rated):
        path, _ = rated
        out = path / "calib"
        invoke(
            path,
            "calibrate",
            "--from-run",
            "latest",
            "--labels",
            str(path / "labels.csv"),
            "--out",
            str(out),
        )
        config = yaml.safe_load((out / "eval.yaml").read_text(encoding="utf-8"))
        types = [criterion["scorer"]["type"] for criterion in config["scorecard"]]
        assert types == ["agreement", "agreement"]

    def test_the_judge_model_is_configurable(self, rated):
        path, _ = rated
        out = path / "calib"
        invoke(
            path,
            "calibrate",
            "--from-run",
            "latest",
            "--labels",
            str(path / "labels.csv"),
            "--out",
            str(out),
            "--judge-model",
            "my-model",
        )
        config = yaml.safe_load((out / "eval.yaml").read_text(encoding="utf-8"))
        assert config["models"][0]["id"] == "my-model"

    def test_it_calls_no_model(self, rated, monkeypatch):
        async def explode(self, request):
            raise AssertionError("calibrate made a model call")

        monkeypatch.setattr("evaling.providers.mock.MockProvider.complete", explode)
        path, _ = rated
        assert (
            invoke(
                path,
                "calibrate",
                "--from-run",
                "latest",
                "--labels",
                str(path / "labels.csv"),
                "--out",
                str(path / "c"),
            ).exit_code
            == 0
        )

    def test_unrated_cases_are_reported_not_hidden(self, rated):
        """Silence here would read as "every case was rated"."""
        path, _ = rated
        (path / "partial.csv").write_text("case_id,human_label\na1,5\n", encoding="utf-8")
        result = invoke(
            path,
            "calibrate",
            "--from-run",
            "latest",
            "--labels",
            str(path / "partial.csv"),
            "--out",
            str(path / "c"),
        )
        assert result.exit_code == 0, result.output
        assert "2 case(s) in the run had no rating" in result.output

    def test_it_refuses_to_write_into_a_non_empty_directory(self, rated):
        path, _ = rated
        out = path / "calib"
        out.mkdir()
        (out / "something.txt").write_text("mine", encoding="utf-8")
        result = invoke(
            path,
            "calibrate",
            "--from-run",
            "latest",
            "--labels",
            str(path / "labels.csv"),
            "--out",
            str(out),
        )
        assert result.exit_code == 2
        assert "not empty" in result.output
        assert (out / "something.txt").read_text(encoding="utf-8") == "mine"

    def test_an_unknown_run_is_an_error(self, rated):
        path, _ = rated
        result = invoke(
            path,
            "calibrate",
            "--from-run",
            "no-such-run",
            "--labels",
            str(path / "labels.csv"),
            "--out",
            str(path / "c"),
        )
        assert result.exit_code == 2
