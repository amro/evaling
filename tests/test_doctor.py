"""`evaling doctor`: the state of an installation, in one command.

Two things it must never do. It must not reach the network unless asked — the
whole point is that you can run it before anything works. And it must not
print a secret: a secrets file's variable *names* are what you need to check,
its values are not.
"""

import json

import pytest
from click.testing import CliRunner

from evaling import diagnostics
from evaling.cli import main

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}

CONFIG = """\
models:
  - {id: mock, provider: mock}
  - {id: judge-m, provider: mock, role: judge}
variants:
  - name: v1
    prompt: [{role: user, content: "{{ q }}"}]
cases: [{id: c1, vars: {q: alpha}}]
scorecard:
  - {criterion: acc, scorer: {type: exact}}
  - {criterion: graded, scorer: {type: llm-judge, judge: j}}
judges:
  j: {model: judge-m, rubric: [{role: user, content: grade}]}
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "eval.yaml").write_text(CONFIG, encoding="utf-8")
    return tmp_path


def invoke(project, *args, env=None):
    return CliRunner().invoke(
        main,
        ["-c", str(project / "eval.yaml"), *args],
        env={**ENV, **(env or {})},
        catch_exceptions=False,
    )


class TestWhatItReports:
    def test_it_names_the_version_python_and_platform(self, project):
        result = invoke(project, "doctor")
        assert result.exit_code == 0, result.output
        from evaling import __version__

        assert __version__ in result.output
        assert "python" in result.output and "platform" in result.output

    def test_it_summarizes_the_config(self, project):
        result = invoke(project, "doctor")
        assert "mock" in result.output
        assert "v1" in result.output
        assert "1 inline" in result.output
        assert "judge-m" in result.output

    def test_every_setting_shows_where_it_came_from(self, project):
        """The answer to "why is it not using what I told it to use"."""
        payload = json.loads(invoke(project, "--json", "doctor").output)
        assert payload["settings"]["concurrency"] == {"value": "8", "from": "default"}

        from_env = json.loads(
            invoke(project, "--json", "doctor", env={"EVALING_CONCURRENCY": "3"}).output
        )
        assert from_env["settings"]["concurrency"] == {
            "value": "3",
            "from": "EVALING_CONCURRENCY",
        }

    def test_a_config_setting_is_attributed_to_the_config(self, project):
        (project / "eval.yaml").write_text(
            CONFIG + "settings: {concurrency: 2}\n", encoding="utf-8"
        )
        payload = json.loads(invoke(project, "--json", "doctor").output)
        assert payload["settings"]["concurrency"]["value"] == "2"
        assert "eval.yaml" in payload["settings"]["concurrency"]["from"]

    def test_a_command_line_setting_is_attributed_to_the_command_line(self, project):
        result = CliRunner().invoke(
            main,
            [
                "-c",
                str(project / "eval.yaml"),
                "-o",
                str(project / "elsewhere"),
                "--json",
                "doctor",
            ],
            env=ENV,
            catch_exceptions=False,
        )
        payload = json.loads(result.output)
        assert payload["settings"]["output_dir"] == {
            "value": str(project / "elsewhere"),
            "from": "command line",
        }

    def test_it_reports_the_cache_and_run_store(self, project):
        payload = json.loads(invoke(project, "--json", "doctor").output)
        assert payload["cache"]["entries"] == 0
        assert payload["runs"]["count"] == 0
        assert payload["runs"]["writable"] is True


class TestItWorksWhenNothingElseDoes:
    """The command you reach for when things are broken has to survive them."""

    def test_a_missing_config_is_a_finding_not_a_crash(self, tmp_path):
        result = CliRunner().invoke(
            main, ["-c", str(tmp_path / "nope.yaml"), "doctor"], env=ENV, catch_exceptions=False
        )
        assert result.exit_code == 1
        assert "not found" in result.output
        # ...and the rest of the report is still there.
        assert "version" in result.output and "storage" in result.output

    def test_a_broken_config_is_reported_with_its_error(self, tmp_path):
        (tmp_path / "eval.yaml").write_text("models: [", encoding="utf-8")
        result = CliRunner().invoke(
            main, ["-c", str(tmp_path / "eval.yaml"), "doctor"], env=ENV, catch_exceptions=False
        )
        assert result.exit_code == 1
        assert "invalid YAML" in result.output
        assert "version" in result.output

    def test_a_missing_api_key_is_a_problem(self, tmp_path):
        (tmp_path / "eval.yaml").write_text(
            "models: [{id: claude, provider: anthropic}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: [{id: c1, vars: {q: hi}}]\n"
            "scorecard: [{criterion: acc, scorer: {type: exact}}]\n",
            encoding="utf-8",
        )
        report = diagnostics.collect(tmp_path / "eval.yaml", env={})
        assert any("ANTHROPIC_API_KEY" in problem for problem in report.problems)
        assert report.sections["models"][0]["api_key_found"] is False

    def test_a_present_api_key_is_not_a_problem(self, tmp_path):
        (tmp_path / "eval.yaml").write_text(
            "models: [{id: claude, provider: anthropic}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: [{id: c1, vars: {q: hi}}]\n"
            "scorecard: [{criterion: acc, scorer: {type: exact}}]\n",
            encoding="utf-8",
        )
        report = diagnostics.collect(
            tmp_path / "eval.yaml", env={"ANTHROPIC_API_KEY": "not-a-real-key"}
        )
        assert report.sections["models"][0]["api_key_found"] is True
        assert not [p for p in report.problems if "ANTHROPIC_API_KEY" in p]

    def test_it_exits_1_on_findings_so_a_script_can_use_it(self, tmp_path):
        result = CliRunner().invoke(
            main, ["-c", str(tmp_path / "nope.yaml"), "doctor"], env=ENV, catch_exceptions=False
        )
        assert result.exit_code == 1


class TestSecretsAreDescribedNotPrinted:
    @pytest.fixture
    def with_secrets(self, project):
        (project / ".evaling.secrets.yaml").write_text(
            "ANTHROPIC_API_KEY: sk-ant-super-secret-value\n"
            "OPENAI_API_KEY: sk-another-secret-value\n",
            encoding="utf-8",
        )
        return project

    def test_the_variable_names_are_shown(self, with_secrets):
        result = invoke(with_secrets, "doctor")
        assert "ANTHROPIC_API_KEY" in result.output
        assert "OPENAI_API_KEY" in result.output
        assert ".evaling.secrets.yaml" in result.output

    def test_the_values_are_not(self, with_secrets):
        text = invoke(with_secrets, "doctor").output
        assert "sk-ant-super-secret-value" not in text
        assert "sk-another-secret-value" not in text

    def test_not_in_json_either(self, with_secrets):
        """The machine-readable form is what gets pasted into issues."""
        text = invoke(with_secrets, "--json", "doctor").output
        assert "sk-ant-super-secret-value" not in text
        assert "sk-another-secret-value" not in text
        payload = json.loads(text)
        assert payload["secrets"]["files"][0]["keys"] == [
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
        ]

    def test_an_unreadable_secrets_file_is_a_problem(self, project):
        (project / ".evaling.secrets.yaml").write_text("- not a mapping\n", encoding="utf-8")
        result = invoke(project, "doctor")
        assert result.exit_code == 1
        assert "expected a mapping" in result.output


class TestItDoesNotTouchTheNetwork:
    def test_no_provider_is_ever_called(self, project, monkeypatch):
        """The whole point is that it runs before anything works."""
        called = []

        async def explode(self, request):
            called.append(request)
            raise AssertionError("doctor made a model call")

        monkeypatch.setattr("evaling.providers.mock.MockProvider.complete", explode)
        assert invoke(project, "doctor").exit_code == 0
        assert not called

    def test_the_probe_is_opt_in_and_separate(self, project, monkeypatch):
        """--check-providers is the only path that spends anything."""
        seen = []

        async def record(self, request):
            seen.append(self.spec.id)
            from evaling.providers.base import Completion

            return Completion(text="pong", cost_usd=0.0)

        monkeypatch.setattr("evaling.providers.mock.MockProvider.complete", record)
        result = invoke(project, "doctor", "--check-providers")
        assert result.exit_code == 0, result.output
        assert seen == ["mock", "judge-m"], "every model should be checked, judges included"
        assert "spends a little money" in result.output
        assert "provider checks" in result.output

    def test_a_failing_probe_is_reported_not_raised(self, project, monkeypatch):
        async def explode(self, request):
            raise RuntimeError("no credentials for you")

        monkeypatch.setattr("evaling.providers.mock.MockProvider.complete", explode)
        result = invoke(project, "--json", "doctor", "--check-providers")
        checks = json.loads(result.output)["provider_checks"]
        assert all(check["reachable"] is False for check in checks)
        assert "no credentials for you" in checks[0]["error"]
