"""The Claude Code plugin is checked like the docs are, for the same reason.

The plugin ships a config reference that an agent reads *instead of* the
schema. If a provider or scorer is added and the reference is not, the agent
is authoritatively told the wrong thing — worse than telling it nothing. These
tests fail when the two drift.

Versions get the same treatment. The plugin's version tracks evaling's, and
the pin in .mcp.json decides which evaling a plugin user actually runs — a pin
that no longer covers the shipped version means the plugin installs something
other than this code.
"""

import json
import re
from pathlib import Path

import pytest

from evaling import __version__
from evaling.config.schema import EvalConfig, ProviderName, ScorerType

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugin"
MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"
MCP_CONFIG = PLUGIN / ".mcp.json"
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
SKILL = PLUGIN / "skills" / "running-evals" / "SKILL.md"
REFERENCE = SKILL.parent / "references" / "config-reference.md"


def literals(annotation) -> set[str]:
    """The allowed strings of a Literal alias."""
    return set(annotation.__args__)


def table_terms(heading: str) -> set[str]:
    """First-column backticked names of the table under a heading.

    Stops at the next heading of any level, so a section's own table is read
    without the subsections beneath it.
    """
    lines = REFERENCE.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index(heading) + 1
    except ValueError:  # pragma: no cover - fails as a missing-section assert
        pytest.fail(f"{REFERENCE.name} has no {heading!r} section")
    terms = set()
    for line in lines[start:]:
        if line.startswith("#"):
            break
        match = re.match(r"\|\s*`([^`]+)`\s*\|", line)
        if match:
            terms.add(match.group(1))
    return terms


def frontmatter(path: Path) -> dict[str, str]:
    """The YAML-ish frontmatter of a skill or command file, as a flat mapping."""
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---", f"{path.name} has no frontmatter"
    end = lines.index("---", 1)
    fields = {}
    for line in lines[1:end]:
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


class TestManifests:
    def test_plugin_manifest_names_the_plugin(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        assert manifest["name"] == "evaling"
        assert manifest["description"]

    def test_marketplace_points_at_the_plugin_directory(self):
        market = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
        (entry,) = market["plugins"]
        assert entry["name"] == "evaling"
        source = MARKETPLACE.parent.parent / entry["source"]
        assert source.resolve() == PLUGIN.resolve()
        assert (source / ".claude-plugin" / "plugin.json").is_file()

    def test_the_server_is_launched_the_documented_way(self):
        servers = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
        assert list(servers) == ["evaling"]
        evaling = servers["evaling"]
        assert evaling["command"] == "uvx"
        # The mcp extra is not installed by a bare `uvx evaling`, and the
        # server is dead without it.
        assert any("evaling[mcp]" in arg for arg in evaling["args"])
        assert evaling["args"][-2:] == ["evaling", "mcp"]


class TestVersions:
    """The plugin carries evaling's version, and launches that version or newer.

    The lower bound is not decoration. The plugin ships from this repository
    while the server comes from PyPI through uvx, which reuses a cached
    environment that already satisfies the requirement rather than resolving
    afresh — so a bound left behind lets a user keep an evaling too old for the
    skill that ships beside it, indefinitely. Raising it every release is both
    the capability guarantee and the only upgrade signal plugin users get.

    The upper bound stays at the next minor, so a patch release is picked up
    without waiting for anything.
    """

    def test_the_manifest_carries_evalings_version(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        assert manifest["version"] == __version__

    def spec(self) -> str:
        servers = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
        for arg in servers["evaling"]["args"]:
            if "evaling[mcp]" in arg:
                return arg
        raise AssertionError("no evaling[mcp] requirement in .mcp.json")

    def test_the_pin_covers_this_version(self):
        spec = self.spec()
        lower = re.search(r">=\s*([0-9.]+)", spec)
        upper = re.search(r"<\s*([0-9.]+)", spec)
        assert lower and upper, f"expected a bounded pin, got {spec!r}"

        def parts(text: str) -> tuple[int, int, int]:
            pieces = [int(piece) for piece in text.split(".")]
            pieces += [0] * (3 - len(pieces))
            return tuple(pieces[:3])

        current = parts(__version__)
        assert parts(lower.group(1)) == current, (
            f"{spec} does not require {__version__}: a user on an older evaling would "
            "keep it, and the plugin describes this version's server"
        )
        assert current < parts(upper.group(1)), f"{spec} excludes {__version__}"


class TestReferenceStaysInSync:
    """Every enum an agent is told about is the one the schema enforces."""

    def test_top_level_keys(self):
        assert table_terms("## Top-level keys") == set(EvalConfig.model_fields)

    def test_providers(self):
        assert table_terms("## Providers") == literals(ProviderName)

    def test_scorer_types(self):
        assert table_terms("## Scorer types") == literals(ScorerType)


class TestSkillAndCommand:
    def test_skill_declares_itself(self):
        fields = frontmatter(SKILL)
        assert fields["name"] == "running-evals"
        assert fields["description"]

    def test_skill_points_at_a_reference_that_exists(self):
        body = SKILL.read_text(encoding="utf-8")
        for name in re.findall(r"`references/([\w.-]+)`", body):
            assert (REFERENCE.parent / name).is_file(), f"missing reference: {name}"

    @pytest.mark.parametrize("command", sorted((PLUGIN / "commands").glob("*.md")))
    def test_commands_declare_a_description(self, command):
        assert frontmatter(command)["description"]

    def test_the_skill_names_every_mcp_tool(self):
        """A tool the skill never mentions is one the agent will not reach for."""
        from evaling import mcp_server

        tools = {name[: -len("_tool")] for name in vars(mcp_server) if name.endswith("_tool")}
        assert tools, "no *_tool implementations found in mcp_server"
        named = set(re.findall(r"`(\w+)`", SKILL.read_text(encoding="utf-8")))
        assert tools <= named, f"skill omits tools: {sorted(tools - named)}"
