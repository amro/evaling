"""The docs are checked like code, because prose rots silently.

Three classes of rot, each cheap to catch and expensive to find by hand:

* a YAML example that no longer matches the schema
* a command or flag that exists but is undocumented (or documented but gone)
* a link to a file that has been moved or renamed
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from evaling.cli import main
from evaling.config import EvalConfig

REPO = Path(__file__).resolve().parent.parent
DOCS = (
    sorted(REPO.glob("docs/*.md"))
    + [REPO / "README.md", REPO / "CONTRIBUTING.md", REPO / "RELEASING.md"]
    # Example READMEs link back into docs/ and are read as often as docs are.
    + sorted(REPO.glob("examples/**/*.md"))
)
REQUIRED_KEYS = {"models", "variants", "cases", "scorecard"}


def code_blocks(path: Path, language: str) -> list[tuple[int, str]]:
    """Every fenced block of one language, with the line it starts on."""
    blocks, current, start = [], None, 0
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if current is None and line.strip() == f"```{language}":
            current, start = [], number
        elif current is not None and line.strip() == "```":
            blocks.append((start, "\n".join(current)))
            current = None
        elif current is not None:
            current.append(line)
    return blocks


def documented_files() -> list[Path]:
    return [path for path in DOCS if path.is_file()]


class TestYamlExamples:
    @pytest.mark.parametrize("path", documented_files(), ids=lambda p: p.name)
    def test_yaml_blocks_parse(self, path):
        for line, block in code_blocks(path, "yaml"):
            # Placeholders like `<run-id>` are prose, not YAML; skip those blocks.
            if "<" in block and ">" in block:
                continue
            try:
                yaml.safe_load(block)
            except yaml.YAMLError as exc:
                pytest.fail(f"{path.name}:{line}: example is not valid YAML: {exc}")

    @pytest.mark.parametrize("path", documented_files(), ids=lambda p: p.name)
    def test_complete_configs_match_the_schema(self, path):
        """Any block with all four required keys must actually validate."""
        checked = 0
        for line, block in code_blocks(path, "yaml"):
            try:
                data = yaml.safe_load(block)
            except yaml.YAMLError:
                continue
            if not isinstance(data, dict) or not set(data) >= REQUIRED_KEYS:
                continue
            # A key-only outline (every value null) documents shape, not content.
            if all(data[key] is None for key in REQUIRED_KEYS):
                continue
            try:
                EvalConfig.model_validate(data)
            except Exception as exc:  # noqa: BLE001 - report any schema complaint
                pytest.fail(f"{path.name}:{line}: config example rejected by the schema: {exc}")
            checked += 1
        if path.name in {"tutorial.md", "configuration.md"}:
            assert checked, f"{path.name} should contain at least one complete config example"


class TestCliDocsMatchReality:
    """docs/cli.md must describe the CLI that exists, not the one that did."""

    def help_for(self, *args) -> str:
        result = CliRunner().invoke(main, [*args, "--help"], catch_exceptions=False)
        assert result.exit_code == 0
        return result.output

    def commands(self) -> list[str]:
        section = self.help_for().split("Commands:")[1]
        return [line.split()[0] for line in section.splitlines() if line.strip()]

    def test_every_command_is_documented(self):
        text = (REPO / "docs" / "cli.md").read_text(encoding="utf-8")
        missing = [name for name in self.commands() if f"`evaling {name}" not in text]
        assert not missing, f"undocumented commands in docs/cli.md: {missing}"

    def test_every_flag_is_documented(self):
        text = (REPO / "docs" / "cli.md").read_text(encoding="utf-8")
        undocumented = []
        for command in self.commands():
            for flag in re.findall(r"^\s+(--[a-z][a-z0-9-]+)", self.help_for(command), re.M):
                if flag not in ("--help",) and flag not in text:
                    undocumented.append(f"{command} {flag}")
        assert not undocumented, f"undocumented flags in docs/cli.md: {undocumented}"

    def test_documented_commands_all_exist(self):
        text = (REPO / "docs" / "cli.md").read_text(encoding="utf-8")
        real = set(self.commands())
        claimed = set(re.findall(r"`evaling ([a-z]+)[ `]", text))
        # Subcommands (`baseline set`) and global-flag examples aren't top-level.
        stale = {name for name in claimed - real if name not in {"set", "show", "info", "clear"}}
        assert not stale, f"docs/cli.md documents commands that don't exist: {stale}"


class TestLinks:
    @pytest.mark.parametrize("path", documented_files(), ids=lambda p: p.name)
    def test_relative_links_resolve(self, path):
        text = path.read_text(encoding="utf-8")
        broken = []
        for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (path.parent / target).exists():
                broken.append(target)
        assert not broken, f"{path.name} links to missing files: {broken}"


def heading_slugs(path: Path) -> set[str]:
    """GitHub's anchor slugs for a file's headings."""
    slugs = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            continue
        title = line.lstrip("#").strip()
        slug = re.sub(r"[^\w\s-]", "", title.lower())
        slugs.add(re.sub(r"[\s_]+", "-", slug).strip("-"))
    return slugs


class TestQuotedErrorMessages:
    """Troubleshooting headings quote errors, and people search for what they saw.

    A heading that no longer matches the real message is worse than no entry:
    the page looks like it covers the problem and the search finds nothing.
    Two headings had already drifted when this was written — one quoted
    Pydantic v1 wording, the other paraphrased a message it never matched.

    What this catches: a heading quoting a message that has been reworded or
    deleted, which is the common case, since a message and its doc entry get
    edited at different times.

    What it does not catch: a paraphrase that happens to share a long run with
    some *other* evaling message. Tying each heading to the specific message it
    documents would need that mapping written down, and keeping the mapping
    current is the same problem this test exists to solve. It is a net, not a
    proof.
    """

    #: Headings quoting something evaling does not produce — a dependency does.
    NOT_OURS = {
        "'question' is undefined",  # jinja2 UndefinedError
        "Extra inputs are not permitted",  # pydantic v2
    }

    #: How much contiguous text a heading must share with a real message. Long
    #: enough that a match is not coincidence, short enough to survive an
    #: interpolated value sitting in the middle of the sentence.
    MIN_RUN = 22

    def message_strings(self) -> str:
        """Every string literal evaling could raise, comments excluded.

        Matching raw file text was too loose: a paraphrase passed because its
        wording appeared in a code comment. Only string literals reach a user.
        """
        chunks: list[str] = []
        for path in sorted((REPO / "src").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = {
                ast.get_docstring(node, clean=False)
                for node in ast.walk(tree)
                if isinstance(
                    node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
                )
            }
            for node in ast.walk(tree):
                if isinstance(node, ast.JoinedStr):
                    # Keep literal runs contiguous across interpolations, so a
                    # message reads here as it reads to the user — but do not
                    # let text join across an interpolated value.
                    chunks.append(
                        "\x00".join(
                            part.value
                            for part in node.values
                            if isinstance(part, ast.Constant) and isinstance(part.value, str)
                        )
                    )
                elif (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value not in docstrings
                ):
                    chunks.append(node.value)
        # Newline-joined so a run can never span two unrelated messages.
        return "\n".join(chunks)

    def longest_shared_run(self, heading: str, source: str) -> int:
        """Longest contiguous slice of the heading appearing in one message."""
        best = 0
        for start in range(len(heading)):
            for end in range(len(heading), start + best, -1):
                if heading[start:end] in source:
                    best = max(best, end - start)
                    break
        return best

    def test_headings_match_real_messages(self):
        text = (REPO / "docs" / "troubleshooting.md").read_text(encoding="utf-8")
        # Backticks are markup in the source strings, not part of the message.
        source = self.message_strings().replace("`", "").lower()
        missing = []
        # Only headings that are entirely a quoted message. A heading like
        # "`result.records` is empty but the run worked" is prose about a
        # symptom, not a string anyone will grep for.
        for heading in re.findall(r"^### `([^`]+)`$", text, re.M):
            if heading in self.NOT_OURS:
                continue
            run = self.longest_shared_run(heading.replace("`", "").lower(), source)
            if run < self.MIN_RUN:
                missing.append(f"{heading!r} (longest shared run: {run} chars)")
        assert not missing, "troubleshooting headings not found in src/: " + "; ".join(missing)

    def test_the_exemptions_are_still_needed(self):
        """If a message becomes ours, it should stop being exempt."""
        source = self.message_strings()
        stale = [message for message in self.NOT_OURS if message.lower() in source.lower()]
        assert not stale, f"now produced by evaling, so no longer exempt: {stale}"


class TestAnchors:
    """A link to a heading that was renamed points nowhere, silently."""

    @pytest.mark.parametrize("path", documented_files(), ids=lambda p: p.name)
    def test_in_page_and_cross_page_anchors_resolve(self, path):
        text = path.read_text(encoding="utf-8")
        broken = []
        for target, anchor in re.findall(r"\]\(([^)#]*)#([^)]+)\)", text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            destination = (path.parent / target) if target else path
            if not destination.is_file() or destination.suffix != ".md":
                continue
            if anchor.lower() not in heading_slugs(destination):
                broken.append(f"{target or path.name}#{anchor}")
        assert not broken, f"{path.name} links to missing headings: {broken}"


class TestExamplesAreReal:
    def test_every_example_directory_has_a_config(self):
        examples = sorted(p for p in (REPO / "examples").iterdir() if p.is_dir())
        assert examples, "examples/ should not be empty"
        for directory in examples:
            assert (directory / "eval.yaml").is_file(), f"{directory.name} has no eval.yaml"

    def test_readme_and_tutorial_point_at_examples_that_exist(self):
        for path in (REPO / "README.md", REPO / "docs" / "tutorial.md"):
            for name in re.findall(r"examples/([a-z-]+)", path.read_text(encoding="utf-8")):
                assert (REPO / "examples" / name).is_dir(), f"{path.name}: no examples/{name}"


class TestInstallInstructions:
    def test_version_matches_the_package(self):
        from evaling import __version__

        output = subprocess.run(
            [sys.executable, "-m", "evaling", "--version"],
            capture_output=True,
            text=True,
            cwd=REPO,
        )
        assert __version__ in output.stdout
