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

    def test_the_sample_bounds_cases_not_cells(self, tmp_path):
        """`--sample N` draws N cases; the whole matrix still runs over each.

        Documented in cli.md, because reading it as a cap on *cells* is the
        natural misreading and gets the bill wrong by the size of the matrix.
        """
        config = (
            "models: [{id: m1, provider: mock}, {id: m2, provider: mock}]\n"
            "variants:\n"
            '  - {name: v1, prompt: [{role: user, content: "{{ q }}"}]}\n'
            '  - {name: v2, prompt: [{role: user, content: "{{ q }}!"}]}\n'
            "cases: [" + ", ".join(f"{{id: c{i}, vars: {{q: '{i}'}}}}" for i in range(20)) + "]\n"
            "scorecard: [{criterion: acc, scorer: {type: contains, value: ''}}]\n"
        )
        (tmp_path / "eval.yaml").write_text(config, encoding="utf-8")
        result = invoke(tmp_path, "run", "--sample", "5")
        assert result.exit_code == 0, result.output
        assert "20 requests" in result.output  # 2 variants × 2 models × 5 cases

        run_id = RunStore(tmp_path / "runs").list_runs()[0]["id"]
        drawn = case_ids(tmp_path, run_id)
        assert len(drawn) == 20
        # The same five cases in every cell, or the cells are not comparable.
        assert len(set(drawn)) == 5

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


class TestComparingRunsThatCoverDifferentCases:
    """A comparison attributes every delta to whatever you changed.

    That reading is only correct if both runs saw the same cases. Comparing a
    sampled run with a full one breaks it silently: the numbers stay entirely
    plausible and part of each delta is just which cases were drawn. Found by
    driving the CLI, where nothing said a word about it.
    """

    def flat(self, result):
        """Console output with wrapping undone, so assertions aren't width-bound."""
        return " ".join(result.output.split())

    def two_runs(self, project, *first, second=()):
        invoke(project, "run", *first)
        invoke(project, "run", *second)
        store = RunStore(project / "runs")
        runs = store.list_runs()
        return runs[-2]["id"], runs[-1]["id"]

    def test_a_sampled_run_against_a_full_one_is_flagged(self, project):
        a, b = self.two_runs(project, "--sample", "5", second=())
        result = invoke(project, "compare", a, b)
        assert result.exit_code == 0, result.output
        assert "cover different cases" in self.flat(result)
        assert "not the change you made" in self.flat(result)

    def test_two_different_draws_are_flagged(self, project):
        a, b = self.two_runs(
            project,
            "--sample",
            "5",
            "--sample-seed",
            "1",
            second=("--sample", "5", "--sample-seed", "2"),
        )
        result = invoke(project, "compare", a, b)
        assert "different samples" in self.flat(result)
        assert "same --sample and --sample-seed" in self.flat(result)

    def test_the_same_draw_twice_is_not_flagged(self, project):
        a, b = self.two_runs(
            project,
            "--sample",
            "5",
            "--sample-seed",
            "7",
            second=("--sample", "5", "--sample-seed", "7"),
        )
        result = invoke(project, "compare", a, b)
        assert "different" not in self.flat(result)

    def test_two_full_runs_are_not_flagged(self, project):
        a, b = self.two_runs(project)
        assert "different" not in self.flat(invoke(project, "compare", a, b))

    def test_the_html_comparison_carries_it_too(self, project, tmp_path):
        """The shared artifact needs the caveat most: it outlives the terminal."""
        a, b = self.two_runs(project, "--sample", "5", second=())
        out = tmp_path / "compare.html"
        assert invoke(project, "compare", a, b, "--html", str(out)).exit_code == 0
        html = out.read_text(encoding="utf-8")
        assert "cover different cases" in html
        # Above the table, where it can still change how the numbers are read.
        assert html.index("cover different cases") < html.index("<table")

    def test_a_sample_covering_everything_is_not_flagged(self, project):
        """--sample 100 over 40 cases takes all 40, which is full coverage."""
        a, b = self.two_runs(project, "--sample", str(CASES * 10), second=())
        assert "different" not in self.flat(invoke(project, "compare", a, b))

    def test_two_full_runs_over_different_cases_are_flagged(self, project):
        """Sampling is the common way here, not the only one.

        Two runs narrowed with different `--case` filters compare exactly as
        misleadingly, and their sizes can match precisely.
        """
        invoke(project, "run", "--case", "c1", "--case", "c2")
        invoke(project, "run", "--case", "c8", "--case", "c9")
        runs = RunStore(project / "runs").list_runs()
        result = invoke(project, "compare", runs[-2]["id"], runs[-1]["id"])
        assert "different sets of cases" in self.flat(result)

    def test_two_full_runs_over_the_same_cases_are_not(self, project):
        invoke(project, "run", "--case", "c1", "--case", "c2")
        invoke(project, "run", "--case", "c1", "--case", "c2")
        runs = RunStore(project / "runs").list_runs()
        result = invoke(project, "compare", runs[-2]["id"], runs[-1]["id"])
        assert "different" not in self.flat(result)

    def test_the_warning_reaches_json_and_mcp(self, project):
        a, b = self.two_runs(project, "--sample", "5", second=())
        payload = json.loads(invoke(project, "--json", "compare", a, b).output)
        assert "cover different cases" in payload["warning"]

        from evaling.mcp_server import compare_runs_tool

        over_mcp = compare_runs_tool(a, b, output_dir=str(project / "runs"))
        assert over_mcp["warning"] == payload["warning"]


class TestResumeRefusesADifferentMatrix:
    """A resumed run must finish the run it started, not a differently-filtered one.

    The config fingerprint covers the config and every file it references, but
    not the flags. `--resume` with a different `--case` used to run whatever
    the new filters selected and finalize the run as complete. With a sample
    it was worse: the draw is by position into the filtered list, so resuming
    over a smaller population produced a hybrid of two draws — a run whose
    cells came from two different case sets, with entirely ordinary-looking
    numbers.
    """

    def settings_for(self, project):
        from evaling.config import Settings

        return Settings.model_validate(
            {"output_dir": str(project / "runs"), "cache": False, "concurrency": 4}
        )

    def interrupted(self, project, **kwargs):
        settings = self.settings_for(project)
        config = load_config(project / "eval.yaml")
        result = run_eval(config, settings, **kwargs)
        rewind(settings.output_dir / result.run_id, keep=2)
        return settings, config, result

    def test_a_narrower_case_filter_is_refused(self, project):
        settings, config, first = self.interrupted(project, sample=5)
        with pytest.raises(StorageError, match="different matrix"):
            run_eval(
                config,
                settings,
                resume_run_id=first.run_id,
                case_filter=[f"c{i}" for i in range(10)],
            )

    def test_a_narrower_variant_or_model_filter_is_refused(self, project):
        """Not only sampling: any filter change finishes a different run."""
        (project / "eval.yaml").write_text(
            CONFIG.replace(
                'variants:\n  - name: v1\n    prompt: [{role: user, content: "{{ q }}"}]\n',
                "variants:\n"
                '  - name: v1\n    prompt: [{role: user, content: "{{ q }}"}]\n'
                '  - name: v2\n    prompt: [{role: user, content: "say {{ q }}"}]\n',
            ),
            encoding="utf-8",
        )
        settings, config, first = self.interrupted(project)
        with pytest.raises(StorageError, match="different matrix"):
            run_eval(config, settings, resume_run_id=first.run_id, variant_filter=["v1"])

    def test_the_message_names_what_changed(self, project):
        settings, config, first = self.interrupted(project, sample=5)
        with pytest.raises(StorageError) as caught:
            run_eval(config, settings, resume_run_id=first.run_id, case_filter=["c1", "c2", "c3"])
        assert "a different set of cases to draw from" in str(caught.value)

    def test_swapping_cases_for_the_same_number_of_others_is_refused(self, project):
        """The half the first version of this guard missed.

        It compared counts, so `--case c0 --case c1` resumed with
        `--case c4 --case c5` was accepted and finalized a run whose cells came
        from two different case sets. Every count was identical.
        """
        settings = self.settings_for(project)
        config = load_config(project / "eval.yaml")
        first = run_eval(config, settings, case_filter=["c0", "c1"])
        rewind(settings.output_dir / first.run_id, keep=1)

        with pytest.raises(StorageError, match="different set of cases"):
            run_eval(config, settings, resume_run_id=first.run_id, case_filter=["c4", "c5"])

    def test_swapping_one_variant_for_another_is_refused(self, project):
        (project / "eval.yaml").write_text(
            CONFIG.replace(
                'variants:\n  - name: v1\n    prompt: [{role: user, content: "{{ q }}"}]\n',
                "variants:\n"
                '  - name: v1\n    prompt: [{role: user, content: "{{ q }}"}]\n'
                '  - name: v2\n    prompt: [{role: user, content: "say {{ q }}"}]\n',
            ),
            encoding="utf-8",
        )
        settings = self.settings_for(project)
        config = load_config(project / "eval.yaml")
        first = run_eval(config, settings, variant_filter=["v1"], sample=3)
        rewind(settings.output_dir / first.run_id, keep=1)

        with pytest.raises(StorageError, match="variants"):
            run_eval(config, settings, resume_run_id=first.run_id, variant_filter=["v2"])

    def test_a_same_sized_population_with_different_cases_is_refused(self, project):
        """A positional draw over a different population picks different cases."""
        settings = self.settings_for(project)
        config = load_config(project / "eval.yaml")
        first_ten = [f"c{i}" for i in range(10)]
        other_ten = [f"c{i}" for i in range(10, 20)]
        first = run_eval(config, settings, case_filter=first_ten, sample=4)
        rewind(settings.output_dir / first.run_id, keep=1)

        with pytest.raises(StorageError, match="different set of cases"):
            run_eval(config, settings, resume_run_id=first.run_id, case_filter=other_ten, sample=4)

    def test_the_same_filters_still_resume(self, project):
        settings, config, first = self.interrupted(project, sample=5)
        resumed = run_eval(config, settings, resume_run_id=first.run_id)
        assert resumed.run_id == first.run_id
        assert resumed.counts["total"] == 5

    def test_an_unsampled_run_still_resumes(self, project):
        settings, config, first = self.interrupted(project)
        resumed = run_eval(config, settings, resume_run_id=first.run_id)
        assert resumed.counts["total"] == CASES

    def test_a_run_from_before_this_check_still_resumes(self, project):
        """Old runs have no recorded matrix; they must not become unresumable."""
        settings, config, first = self.interrupted(project)
        meta_path = settings.output_dir / first.run_id / "run.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        del meta["matrix"]
        meta_path.write_text(json.dumps(meta), encoding="utf-8", newline="\n")

        assert run_eval(config, settings, resume_run_id=first.run_id).counts["total"] == CASES
