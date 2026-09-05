"""Two things a tool must not do to files it was pointed at.

Both were found by a full-codebase audit, and both are the kind that only
matter once: `--log-requests eval.yaml` destroys the config, and a dataset
that names `file://../../../.ssh/id_rsa` gets that file read, hashed, sent to
a model API, and archived in the run directory.
"""

import asyncio
import json

import pytest
from click.testing import CliRunner

from evaling.cli import main
from evaling.config import load_config
from evaling.config.cases import load_cases
from evaling.config.errors import ConfigError
from evaling.content import ContentError
from evaling.errors import EvalingError
from evaling.render import render_messages
from evaling.reqlog import RequestLog

CONFIG_HEAD = (
    "models: [{id: mock, provider: mock}]\n"
    "variants:\n  - name: v1\n"
    '    prompt: [{role: user, content: "{{ q }}"}]\n'
)
SCORECARD = 'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n'
ENV = {"EVALING_USER_CONFIG": "/nonexistent"}


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
        RequestLog(log).record(model="old")
        assert log.stat().st_size > 0
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

    def test_a_case_variable_cannot_be_used_as_a_media_path(self, tmp_path):
        """The other way a dataset can name a file: skip `files` entirely.

        Containment happens when a case loads, and only `files` entries go
        through it. A prompt that templates a plain case var into a media part
        — `{{ photo }}` rather than `{{ files.photo }}` — resolved wherever
        the dataset said, and the file was read, hashed, sent to the model API
        and archived, which is what containment exists to stop.
        """
        outside = tmp_path / "elsewhere.png"
        outside.write_bytes(b"secret-bytes")
        project = tmp_path / "project"
        project.mkdir()
        (project / "cases.jsonl").write_text(
            json.dumps({"id": "c1", "q": "a", "photo": str(outside)}) + "\n", encoding="utf-8"
        )
        (project / "eval.yaml").write_text(
            "models: [{id: m, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: [{text: "{{ q }}"}, {image: "{{ photo }}"}]}]\n'
            "cases: {file: cases.jsonl}\n" + SCORECARD,
            encoding="utf-8",
        )
        config = load_config(project / "eval.yaml")
        [case] = load_cases(config)
        with pytest.raises(ContentError, match="comes from case data"):
            render_messages(config.variants[0].prompt, case, config.base_dir)

    def test_a_media_path_written_into_the_config_still_reaches_outside(self, tmp_path):
        """A literal in the prompt is the config author's own path."""
        outside = tmp_path / "elsewhere.png"
        outside.write_bytes(b"deliberate")
        project = tmp_path / "project"
        project.mkdir()
        (project / "eval.yaml").write_text(
            "models: [{id: m, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            # Single-quoted: a Windows path is full of backslashes, and inside
            # a double-quoted YAML scalar `C:\Users\...` is read as escape
            # sequences — `\U` starts a unicode escape and fails to parse.
            f"    prompt: [{{role: user, content: [{{image: '{outside}'}}]}}]\n"
            "cases: [{id: c1, vars: {q: a}}]\n" + SCORECARD,
            encoding="utf-8",
        )
        config = load_config(project / "eval.yaml")
        [case] = load_cases(config)
        [message] = render_messages(config.variants[0].prompt, case, config.base_dir)
        assert message.parts[0].path == outside.resolve()

    def test_a_files_attachment_still_reaches_the_prompt(self, tmp_path):
        """`files.*` is contained at load time, so it is trusted here."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "pic.png").write_bytes(b"fine")
        (project / "cases.jsonl").write_text(
            json.dumps({"id": "c1", "q": "a", "photo": "file://pic.png"}) + "\n", encoding="utf-8"
        )
        (project / "eval.yaml").write_text(
            "models: [{id: m, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: [{image: "{{ files.photo }}"}]}]\n'
            "cases: {file: cases.jsonl}\n" + SCORECARD,
            encoding="utf-8",
        )
        config = load_config(project / "eval.yaml")
        [case] = load_cases(config)
        [message] = render_messages(config.variants[0].prompt, case, config.base_dir)
        assert message.parts[0].path == (project / "pic.png").resolve()

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


class TestASourceCannotReachOutsideTheProject:
    """The rows a source returns are data, even though its code is trusted.

    Containment covered inline cases and dataset files; source-yielded cases
    reached the engine without passing through it at all. A source usually
    fetches from an API or a warehouse, so a `files` value in a row is
    untrusted input arriving through trusted code — the dataset threat model,
    by a different route. The same path that a CSV is refused for was read,
    sent to the model API, and archived with the run.
    """

    SOURCE = (
        "from evaling import Case, CasePage\n"
        "import os\n"
        "class S:\n"
        "    def fetch(self, cursor, limit):\n"
        "        row = Case(id='c1', vars={'q': 'a'}, files={'doc': os.environ['DOC']})\n"
        "        return CasePage(cases=[row], cursor=None)\n"
        "def make():\n"
        "    return S()\n"
    )

    def project(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "src.py").write_text(self.SOURCE, encoding="utf-8")
        (project / "inside.pdf").write_bytes(b"fine")
        (tmp_path / "outside.pdf").write_bytes(b"secret")
        (project / "eval.yaml").write_text(
            "models: [{id: m, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: [{text: "{{ q }}"}, {file: "{{ files.doc }}"}]}]\n'
            "cases: {source: 'src.py:make', limit: 5}\n" + SCORECARD,
            encoding="utf-8",
        )
        return project

    def cases_from(self, project, doc, monkeypatch):
        from evaling.config import load_config
        from evaling.sources import iter_source_cases, load_source

        monkeypatch.setenv("DOC", doc)
        config = load_config(project / "eval.yaml")
        source = load_source(config.cases.source, config.base_dir, config.cases.params)

        async def go():
            return [c async for c in iter_source_cases(source, 10, 10, config.base_dir)]

        return asyncio.run(go())

    @pytest.mark.parametrize("doc", ["../outside.pdf", "{absolute}"])
    def test_a_row_naming_a_file_outside_is_refused(self, tmp_path, monkeypatch, doc):
        project = self.project(tmp_path)
        doc = doc.format(absolute=tmp_path / "outside.pdf")
        with pytest.raises(ConfigError, match="resolves outside"):
            self.cases_from(project, doc, monkeypatch)

    def test_a_row_naming_a_file_inside_still_works(self, tmp_path, monkeypatch):
        project = self.project(tmp_path)
        [case] = self.cases_from(project, "inside.pdf", monkeypatch)
        assert case.files["doc"] == str((project / "inside.pdf").resolve())

    def test_the_whole_run_refuses_rather_than_reading_the_file(self, tmp_path, monkeypatch):
        """End to end: the file must not be read, hashed, or archived."""
        project = self.project(tmp_path)
        monkeypatch.setenv("DOC", str(tmp_path / "outside.pdf"))
        monkeypatch.chdir(project)
        result = CliRunner().invoke(
            main, ["-c", str(project / "eval.yaml"), "run"], env=ENV, catch_exceptions=False
        )
        assert result.exit_code != 0
        archived = (
            list((project / ".evaling").rglob("*.pdf")) if (project / ".evaling").is_dir() else []
        )
        assert not archived, f"the outside file was archived: {archived}"


class TestSymlinksCannotWalkOutEither:
    """Both gates `.resolve()` before the parent check, so a symlink is caught.

    Nothing pinned that. A refactor moving the containment check ahead of the
    resolve — an easy thing to do while tidying — reopens the escape silently,
    and a symlink inside the project is the ordinary way to arrange one.
    """

    def project_with_link(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.pdf").write_bytes(b"secret")
        project = tmp_path / "project"
        project.mkdir()
        (project / "link.pdf").symlink_to(outside / "secret.pdf")
        return project

    def test_a_dataset_cannot_reach_out_through_a_symlink(self, tmp_path):
        project = self.project_with_link(tmp_path)
        (project / "cases.jsonl").write_text(
            json.dumps({"id": "c1", "q": "a", "doc": "file://link.pdf"}) + "\n", encoding="utf-8"
        )
        (project / "eval.yaml").write_text(
            CONFIG_HEAD + "cases: {file: cases.jsonl}\n" + SCORECARD, encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="resolves outside"):
            load_cases(load_config(project / "eval.yaml"))

    def test_a_case_variable_cannot_reach_out_through_a_symlink(self, tmp_path):
        project = self.project_with_link(tmp_path)
        (project / "cases.jsonl").write_text(
            json.dumps({"id": "c1", "q": "a", "doc": "link.pdf"}) + "\n", encoding="utf-8"
        )
        (project / "eval.yaml").write_text(
            "models: [{id: m, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: [{text: "{{ q }}"}, {file: "{{ doc }}"}]}]\n'
            "cases: {file: cases.jsonl}\n" + SCORECARD,
            encoding="utf-8",
        )
        config = load_config(project / "eval.yaml")
        [case] = load_cases(config)
        with pytest.raises(ContentError, match="comes from case data"):
            render_messages(config.variants[0].prompt, case, config.base_dir)

    def test_a_case_variable_naming_a_file_inside_still_renders(self, tmp_path):
        """The other side of that branch, which no test took.

        A regression rejecting *all* case-variable media, not just escaping
        ones, would have passed every containment test.
        """
        project = tmp_path / "project"
        project.mkdir()
        (project / "inside.pdf").write_bytes(b"fine")
        (project / "cases.jsonl").write_text(
            json.dumps({"id": "c1", "q": "a", "doc": "inside.pdf"}) + "\n", encoding="utf-8"
        )
        (project / "eval.yaml").write_text(
            "models: [{id: m, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: [{text: "{{ q }}"}, {file: "{{ doc }}"}]}]\n'
            "cases: {file: cases.jsonl}\n" + SCORECARD,
            encoding="utf-8",
        )
        config = load_config(project / "eval.yaml")
        [case] = load_cases(config)
        [message] = render_messages(config.variants[0].prompt, case, config.base_dir)
        assert message.parts[1].path == (project / "inside.pdf").resolve()
