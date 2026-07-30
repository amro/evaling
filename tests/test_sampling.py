"""`--sample N`: a random subset of the cases, repeatably.

The fast loop while a prompt is still moving. The properties that matter are
that the draw is genuinely random, that it can be repeated from its seed, and
that a resumed run continues the draw it started with rather than making a new
one — a run whose halves cover different cases produces no error and plausible
numbers.
"""

import json

import pytest
from click.testing import CliRunner

from evaling.cli import main
from evaling.config import load_config
from evaling.engine import run_eval, sample_cases, select_matrix
from evaling.storage import RunStore, StorageError

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}

CASES = 40

CONFIG = (
    "models: [{id: mock, provider: mock}]\n"
    "variants:\n  - name: v1\n"
    '    prompt: [{role: user, content: "{{ q }}"}]\n'
    "cases: [" + ", ".join(f"{{id: c{i}, vars: {{q: '{i}'}}}}" for i in range(CASES)) + "]\n"
    "scorecard: [{criterion: acc, scorer: {type: contains, value: ''}}]\n"
)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "eval.yaml").write_text(CONFIG, encoding="utf-8")
    return tmp_path


def invoke(project, *args):
    return CliRunner().invoke(
        main,
        ["-c", str(project / "eval.yaml"), "-o", str(project / "runs"), *args],
        env=ENV,
        catch_exceptions=False,
    )


def case_ids(project, run_id):
    return [record.case_id for record in RunStore(project / "runs").load_results(run_id)]


def rewind(run_path, *, keep):
    """Put a finished run back into the state an interruption leaves it in."""
    results = run_path / "results.jsonl"
    lines = results.read_text(encoding="utf-8").splitlines(keepends=True)
    results.write_text("".join(lines[:keep]), encoding="utf-8", newline="\n")
    meta = json.loads((run_path / "run.json").read_text(encoding="utf-8"))
    meta["status"] = "running"
    (run_path / "run.json").write_text(json.dumps(meta), encoding="utf-8", newline="\n")


class TestTheDraw:
    def test_the_same_seed_draws_the_same_cases(self, project):
        config = load_config(project / "eval.yaml")
        _, _, all_cases = select_matrix(config)
        first = [case.id for case in sample_cases(all_cases, 7, 1234)]
        second = [case.id for case in sample_cases(all_cases, 7, 1234)]
        assert first == second and len(first) == 7

    def test_different_seeds_draw_differently(self, project):
        config = load_config(project / "eval.yaml")
        _, _, all_cases = select_matrix(config)
        draws = {tuple(c.id for c in sample_cases(all_cases, 7, seed)) for seed in range(20)}
        assert len(draws) > 1, "the seed does not affect the draw"

    def test_the_draw_keeps_the_original_order(self, project):
        """So two runs of one draw line up when read side by side."""
        config = load_config(project / "eval.yaml")
        _, _, all_cases = select_matrix(config)
        order = [case.id for case in all_cases]
        drawn = [case.id for case in sample_cases(all_cases, 9, 99)]
        assert drawn == [case_id for case_id in order if case_id in set(drawn)]

    def test_asking_for_more_than_exist_takes_them_all(self, project):
        config = load_config(project / "eval.yaml")
        _, _, all_cases = select_matrix(config)
        assert len(sample_cases(all_cases, CASES * 10, 1)) == CASES

    def test_sampling_composes_with_case_filters(self, project):
        """The sample is drawn from what the other filters left."""
        config = load_config(project / "eval.yaml")
        _, _, chosen = select_matrix(config, cases=["c1", "c2", "c3"], sample=2, sample_seed=5)
        assert len(chosen) == 2
        assert {case.id for case in chosen} <= {"c1", "c2", "c3"}


class TestThroughTheCli:
    def test_only_the_sampled_cases_run(self, project):
        result = invoke(project, "run", "--sample", "6")
        assert result.exit_code == 0, result.output
        assert "6 requests" in result.output
        assert "sampling 6 of 40 cases" in result.output

        run_id = RunStore(project / "runs").list_runs()[0]["id"]
        assert len(case_ids(project, run_id)) == 6

    def test_the_seed_is_reported_so_the_draw_can_be_repeated(self, project):
        first = invoke(project, "run", "--sample", "6")
        assert "repeat this draw with" in first.output

        payload = json.loads(invoke(project, "--json", "run", "--sample", "6").output)
        seed = payload["selection"]["seed"]
        assert isinstance(seed, int)
        assert payload["selection"] == {"sample": 6, "seed": seed, "available": 40}

        repeat = json.loads(
            invoke(project, "--json", "run", "--sample", "6", "--sample-seed", str(seed)).output
        )
        store = RunStore(project / "runs")
        assert sorted(case_ids(project, payload["run_id"])) == sorted(
            case_ids(project, repeat["run_id"])
        )
        assert store.load_meta(repeat["run_id"])["selection"]["seed"] == seed

    def test_an_unsampled_run_records_no_selection(self, project):
        payload = json.loads(invoke(project, "--json", "run").output)
        assert payload["selection"] is None
        assert payload["counts"]["total"] == CASES

    def test_a_dry_run_samples_too(self, project):
        result = invoke(project, "run", "--dry-run", "--sample", "5")
        assert result.exit_code == 0, result.output
        assert "5 requests" in result.output

    def test_validate_samples_too(self, project):
        result = invoke(project, "validate", "--sample", "5")
        assert result.exit_code == 0, result.output
        assert "5 requests" in result.output

    @pytest.mark.parametrize("value", ["0", "-3"])
    def test_a_nonsense_sample_is_refused(self, project, value):
        result = invoke(project, "run", "--sample", value)
        assert result.exit_code == 2
        assert "at least 1" in result.output

    def test_a_seed_without_a_sample_is_refused(self, project):
        """Accepting it silently would look like the draw had been pinned."""
        result = invoke(project, "run", "--sample-seed", "7")
        assert result.exit_code == 2
        assert "no effect without --sample" in result.output


class TestResumeKeepsTheDraw:
    """A resumed run must finish the sample it started, not draw a new one."""

    def settings_for(self, project):
        from evaling.config import Settings

        return Settings.model_validate(
            {
                "output_dir": str(project / "runs"),
                "cache_dir": str(project / "cache"),
                "cache": False,
                "concurrency": 4,
            }
        )

    def partial_run(self, project):
        """A sampled run, artificially left incomplete."""
        settings = self.settings_for(project)
        config = load_config(project / "eval.yaml")
        result = run_eval(config, settings, sample=8)
        # Rewind it to what an interruption leaves behind: some of the
        # results, and a status that isn't "complete".
        rewind(settings.output_dir / result.run_id, keep=3)
        return settings, config, result

    def test_resume_finishes_the_original_sample(self, project):
        settings, config, first = self.partial_run(project)
        # The full draw as it was before the interruption -- not what survives
        # on disk, which is only the part that had finished.
        original = {record.case_id for record in first.records}
        assert len(original) == 8

        resumed = run_eval(config, settings, resume_run_id=first.run_id)
        assert resumed.counts["total"] == 8
        assert set(case_ids(project, first.run_id)) == original, (
            "resume drew a different sample, so the run covers two case sets"
        )

    def test_resume_with_a_conflicting_sample_is_refused(self, project):
        settings, config, first = self.partial_run(project)
        with pytest.raises(StorageError, match="cannot be resumed with a sample"):
            run_eval(config, settings, resume_run_id=first.run_id, sample=20)

    def test_resume_with_a_conflicting_seed_is_refused(self, project):
        settings, config, first = self.partial_run(project)
        seed = first.selection["seed"]
        with pytest.raises(StorageError, match="not 999"):
            run_eval(config, settings, resume_run_id=first.run_id, sample=8, sample_seed=999)
        assert seed != 999

    def test_sampling_an_unsampled_run_on_resume_is_refused(self, project):
        settings = self.settings_for(project)
        config = load_config(project / "eval.yaml")
        result = run_eval(config, settings)
        rewind(settings.output_dir / result.run_id, keep=3)
        with pytest.raises(StorageError, match="did not sample"):
            run_eval(config, settings, resume_run_id=result.run_id, sample=5)


class TestSourceBackedRunsRefuseIt:
    """There is no population to draw from when cases arrive a page at a time."""

    @pytest.fixture
    def sourced(self, tmp_path):
        (tmp_path / "src.py").write_text(
            "from evaling import Case, CasePage\n"
            "class S:\n"
            "    def fetch(self, cursor, limit):\n"
            "        start = int(cursor or 0)\n"
            "        rows = [Case(id=f'c{i}', vars={'q': str(i)}) "
            "for i in range(start, min(start + limit, 12))]\n"
            "        nxt = None if start + limit >= 12 else str(start + limit)\n"
            "        return CasePage(cases=rows, cursor=nxt)\n"
            "def make():\n"
            "    return S()\n",
            encoding="utf-8",
        )
        (tmp_path / "eval.yaml").write_text(
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: {source: 'src.py:make', page_size: 4, limit: 8}\n"
            "scorecard: [{criterion: acc, scorer: {type: contains, value: ''}}]\n",
            encoding="utf-8",
        )
        return tmp_path

    def test_the_message_points_at_limit(self, sourced):
        result = invoke(sourced, "run", "--sample", "3")
        assert result.exit_code == 2
        assert "sampling cannot narrow a source-backed run" in result.output
        assert "limit" in result.output

    def test_validate_refuses_it_too(self, sourced):
        result = invoke(sourced, "validate", "--sample", "3")
        assert result.exit_code == 2
        assert "source-backed run" in result.output
