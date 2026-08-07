"""Two things a tool must not do to files it was pointed at.

Both were found by a full-codebase audit, and both are the kind that only
matter once: `--log-requests eval.yaml` destroys the config, and a dataset
that names `file://../../../.ssh/id_rsa` gets that file read, hashed, sent to
a model API, and archived in the run directory.
"""

import json

import pytest

from evaling.config import load_config
from evaling.config.cases import load_cases
from evaling.config.errors import ConfigError
from evaling.errors import EvalingError
from evaling.reqlog import RequestLog

CONFIG_HEAD = (
    "models: [{id: mock, provider: mock}]\n"
    "variants:\n  - name: v1\n"
    '    prompt: [{role: user, content: "{{ q }}"}]\n'
)
SCORECARD = 'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n'


class TestTheRequestLogWillNotClobber:
    def test_it_refuses_a_file_that_is_not_a_log(self, tmp_path):
        config = tmp_path / "eval.yaml"
        original = CONFIG_HEAD + "cases: [{id: c1, vars: {q: a}}]\n" + SCORECARD
        config.write_text(original, encoding="utf-8")

        with pytest.raises(EvalingError, match="refusing to overwrite"):
            RequestLog(config)
        assert config.read_text(encoding="utf-8") == original, "the file was destroyed anyway"

    def test_it_still_truncates_its_own_log(self, tmp_path):
        """Each run starts a fresh trace; a previous one is ours to replace."""
        log = tmp_path / "trace.jsonl"
        log.write_text('{"model": "old"}\n', encoding="utf-8")
        RequestLog(log)
        assert log.read_text(encoding="utf-8") == ""

    def test_an_empty_file_is_fine(self, tmp_path):
        log = tmp_path / "trace.jsonl"
        log.touch()
        RequestLog(log)

    def test_a_new_file_is_fine(self, tmp_path):
        RequestLog(tmp_path / "nested" / "trace.jsonl")
        assert (tmp_path / "nested" / "trace.jsonl").is_file()

    def test_the_message_says_what_to_do(self, tmp_path):
        target = tmp_path / "notes.txt"
        target.write_text("some notes\n", encoding="utf-8")
        with pytest.raises(EvalingError) as caught:
            RequestLog(target)
        assert "point --log-requests at a new file" in str(caught.value)


class TestADatasetCannotReachOutsideItself:
    def project(self, tmp_path, attachment):
        (tmp_path / "secret.txt").write_text("private", encoding="utf-8")
        data = tmp_path / "data"
        data.mkdir()
        (data / "cases.jsonl").write_text(
            json.dumps({"id": "c1", "q": "a", "doc": f"file://{attachment}"}) + "\n",
            encoding="utf-8",
        )
        (tmp_path / "eval.yaml").write_text(
            CONFIG_HEAD + "cases: {file: data/cases.jsonl}\n" + SCORECARD, encoding="utf-8"
        )
        return tmp_path

    def test_a_traversal_is_refused(self, tmp_path):
        path = self.project(tmp_path, "../secret.txt")
        with pytest.raises(ConfigError, match="resolves outside"):
            load_cases(load_config(path / "eval.yaml"))

    def test_a_deeper_traversal_is_refused(self, tmp_path):
        path = self.project(tmp_path, "../../../../../../etc/passwd")
        with pytest.raises(ConfigError, match="resolves outside"):
            load_cases(load_config(path / "eval.yaml"))

    def test_the_message_names_the_way_out(self, tmp_path):
        path = self.project(tmp_path, "../secret.txt")
        with pytest.raises(ConfigError) as caught:
            load_cases(load_config(path / "eval.yaml"))
        # Not "use an absolute path" — that is the escape this closed.
        assert "inline case" in str(caught.value)

    def test_a_path_beside_the_dataset_still_works(self, tmp_path):
        path = self.project(tmp_path, "doc.pdf")
        (path / "data" / "doc.pdf").write_text("fine", encoding="utf-8")
        [case] = load_cases(load_config(path / "eval.yaml"))
        assert case.files["doc"].endswith("doc.pdf")

    def test_a_subdirectory_still_works(self, tmp_path):
        path = self.project(tmp_path, "sub/doc.pdf")
        (path / "data" / "sub").mkdir()
        (path / "data" / "sub" / "doc.pdf").write_text("fine", encoding="utf-8")
        [case] = load_cases(load_config(path / "eval.yaml"))
        assert case.files["doc"].endswith("doc.pdf")

    def test_an_absolute_path_is_refused_too(self, tmp_path):
        """The traversal check was skipped entirely by writing the path absolute.

        This test used to assert the opposite, on the premise that an absolute
        path "is a deliberate choice by whoever wrote the config" — but a
        dataset row is written by whoever wrote the *dataset*, which is the
        thing being defended against. `/home/you/.ssh/id_rsa` in a vendor CSV
        was read, sent to the model API, and archived with the run.
        """
        outside = tmp_path / "elsewhere.pdf"
        outside.write_text("deliberate", encoding="utf-8")
        path = self.project(tmp_path, str(outside))
        with pytest.raises(ConfigError, match="resolves outside"):
            load_cases(load_config(path / "eval.yaml"))

    def test_an_absolute_path_inside_the_dataset_directory_is_fine(self, tmp_path):
        """Contained is contained; how the path is spelled does not matter."""
        path = self.project(tmp_path, "placeholder")
        inside = path / "data" / "doc.pdf"
        inside.write_text("fine", encoding="utf-8")
        (path / "data" / "cases.jsonl").write_text(
            json.dumps({"id": "c1", "q": "a", "doc": f"file://{inside}"}) + "\n",
            encoding="utf-8",
        )
        [case] = load_cases(load_config(path / "eval.yaml"))
        assert case.files["doc"] == str(inside.resolve())

    def test_an_inline_case_may_still_reach_outside(self, tmp_path):
        """The config's author can already point evaling anywhere."""
        outside = tmp_path / "elsewhere.pdf"
        outside.write_text("deliberate", encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir()
        (project / "eval.yaml").write_text(
            CONFIG_HEAD
            + f"cases: [{{id: c1, vars: {{q: a}}, files: {{doc: '{outside}'}}}}]\n"
            + SCORECARD,
            encoding="utf-8",
        )
        [case] = load_cases(load_config(project / "eval.yaml"))
        assert case.files["doc"] == str(outside.resolve())

    def test_inline_cases_are_bounded_by_the_config_directory(self, tmp_path):
        (tmp_path / "secret.txt").write_text("private", encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir()
        (project / "eval.yaml").write_text(
            CONFIG_HEAD
            + "cases: [{id: c1, vars: {q: a}, files: {doc: '../secret.txt'}}]\n"
            + SCORECARD,
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="resolves outside"):
            load_cases(load_config(project / "eval.yaml"))
