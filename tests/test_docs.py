"""The docs are checked like code, because prose rots silently.

Three classes of rot, each cheap to catch and expensive to find by hand:

* a YAML example that no longer matches the schema
* a command or flag that exists but is undocumented (or documented but gone)
* a link to a file that has been moved or renamed
"""

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
DOCS = sorted(REPO.glob("docs/*.md")) + [REPO / "README.md", REPO / "CONTRIBUTING.md"]
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
