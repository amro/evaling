"""Reading user-authored files: the parts a message depends on.

`textfile` exists so that every way a hand-typed file can be wrong is a
sentence rather than a traceback, and the suite covered that it *is* a
sentence. What it did not cover was whether the sentence is right — mutation
testing shifted the reported column by two and moved the BOM handling to a
plain UTF-8 read, and nothing failed.

Both matter to somebody staring at a file trying to find the problem: a
column that points at the wrong character costs more time than no column at
all, and a BOM is invisible in an editor.
"""

import pytest

from evaling.errors import EvalingError
from evaling.textfile import read_text, read_yaml

#: What an editor writes and never shows you.
BOM = b"\xef\xbb\xbf"


class TestABomIsNotContent:
    """`utf-8-sig`, not `utf-8`.

    A byte-order mark is invisible to whoever saved the file and turns a
    CSV's first column into `\\ufeffquestion`, so `{{ question }}` then fails
    as undefined — an error pointing at the template rather than the file.
    """

    def test_a_leading_bom_is_stripped(self, tmp_path):
        path = tmp_path / "cases.csv"
        path.write_bytes(BOM + b"question,expected\na,b\n")
        assert read_text(path, EvalingError, missing="x").startswith("question,")

    def test_a_bom_does_not_reach_a_parsed_key(self, tmp_path):
        path = tmp_path / "eval.yaml"
        path.write_bytes(BOM + b"models: [{id: m}]\n")
        assert read_yaml(path, EvalingError, missing="x") == {"models": [{"id": "m"}]}

    def test_a_file_without_one_is_unchanged(self, tmp_path):
        path = tmp_path / "plain.txt"
        path.write_text("question,expected\n", encoding="utf-8")
        assert read_text(path, EvalingError, missing="x") == "question,expected\n"


class TestWherePyyamlSaysTheProblemIs:
    """Positions are reported 1-based, the way an editor counts.

    Only on the `quote_content=False` path — a secrets file, where the line
    itself cannot be echoed because it holds a credential. The position is
    then the only thing the reader gets, so it has to be right.
    """

    #: Line 3 is the malformed one: a mapping value on an already-open mapping.
    BROKEN = "FIRST: ok\nSECOND: ok\n  BAD_INDENT: here\n"

    def error_for(self, tmp_path, body):
        path = tmp_path / "secrets.yaml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(EvalingError) as caught:
            read_yaml(path, EvalingError, missing="x", quote_content=False)
        return str(caught.value)

    def test_the_line_and_column_are_one_based(self, tmp_path):
        import yaml

        message = self.error_for(tmp_path, self.BROKEN)
        # Against PyYAML's own 0-based mark, so this asserts the conversion
        # rather than restating a number that could drift with the library.
        try:
            yaml.safe_load(self.BROKEN)
        except yaml.YAMLError as exc:
            mark = exc.problem_mark
        assert f"line {mark.line + 1}, column {mark.column + 1}" in message

    def test_the_secret_line_is_not_echoed(self, tmp_path):
        message = self.error_for(tmp_path, "KEY: sk-ant-a-real-looking-credential\n  BAD: x\n")
        assert "sk-ant-a-real-looking-credential" not in message

    @pytest.mark.parametrize(
        "body",
        [
            # An unquoted value starting with `*` is YAML alias syntax, and the
            # parse error names the alias — printing the key from the message
            # that exists to avoid printing it.
            "MY_KEY: *sk-ant-a-real-looking-credential\n",
            # Anchors quote the name back the same way.
            "a: &sk-ant-a-real-looking-credential 1\nb: &sk-ant-a-real-looking-credential 2\n",
        ],
    )
    def test_a_secret_inside_pyyamls_own_problem_text_is_not_echoed(self, tmp_path, body):
        message = self.error_for(tmp_path, body)
        assert "sk-ant-a-real-looking-credential" not in message, message
        assert "line" in message, "the position is what makes this fixable"

    def test_it_still_says_what_was_wrong(self, tmp_path):
        assert "invalid YAML" in self.error_for(tmp_path, self.BROKEN)

    def test_it_repeats_pyyamls_own_description_of_the_problem(self, tmp_path):
        """Without it the message is a position and nothing else."""
        import yaml

        try:
            yaml.safe_load(self.BROKEN)
        except yaml.YAMLError as exc:
            problem = exc.problem
        assert problem
        assert problem in self.error_for(tmp_path, self.BROKEN)


class TestQuotingIsTheDefault:
    """Only a secrets file opts out; a config error should show the line.

    The flag defaults to quoting because for every file except a secrets file
    the offending text is the most useful thing in the message.
    """

    def test_a_config_error_quotes_the_offending_content(self, tmp_path):
        path = tmp_path / "eval.yaml"
        path.write_text("models: [{id: m}\nvariants: oops\n", encoding="utf-8")
        with pytest.raises(EvalingError) as caught:
            read_yaml(path, EvalingError, missing="x")
        assert "variants" in str(caught.value), "the message did not quote the content"


class TestTheOtherFailures:
    def test_a_missing_file_gets_the_caller_s_own_message(self, tmp_path):
        with pytest.raises(EvalingError, match="case file not found"):
            read_text(tmp_path / "gone.csv", EvalingError, missing="case file not found")

    def test_a_non_utf8_file_names_the_offending_byte(self, tmp_path):
        path = tmp_path / "latin.yaml"
        path.write_bytes(b"caf\xe9: yes\n")
        with pytest.raises(EvalingError, match="0xe9"):
            read_text(path, EvalingError, missing="x")

    def test_an_unreadable_path_names_it(self, tmp_path):
        """A directory where a file was expected: an OSError, not a decode error."""
        directory = tmp_path / "a-directory"
        directory.mkdir()
        with pytest.raises(EvalingError, match="a-directory"):
            read_text(directory, EvalingError, missing="x")

    def test_nesting_deeper_than_the_stack_is_a_sentence(self, tmp_path):
        path = tmp_path / "deep.yaml"
        path.write_text("[" * 60_000, encoding="utf-8")
        with pytest.raises(EvalingError, match="nested too deeply"):
            read_yaml(path, EvalingError, missing="x")
