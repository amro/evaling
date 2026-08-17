"""The docs are checked like code, because prose rots silently.

Three classes of rot, each cheap to catch and expensive to find by hand:

* a YAML example that no longer matches the schema
* a command or flag that exists but is undocumented (or documented but gone)
* a documented invocation that the CLI would refuse to parse
* a link to a file that has been moved or renamed
"""

import ast
import json
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


class TestJudgeSnippetsTeachTheRole:
    """A partial YAML block can teach a config that will not load.

    `test_complete_configs_match_the_schema` only validates blocks with all
    four required top-level keys, so a `judges:` fragment slips past it. That
    is how the tutorial came to show a judge config that the schema now
    rejects: a judge's model must declare `role`.
    """

    @pytest.mark.parametrize("path", documented_files(), ids=lambda p: p.name)
    def test_a_judges_block_shows_the_role_it_requires(self, path):
        offenders = []
        for line, block in code_blocks(path, "yaml"):
            data = None
            try:
                data = yaml.safe_load(block)
            except yaml.YAMLError:
                continue
            if not isinstance(data, dict) or not data.get("judges"):
                # `judges:` with no value is a key-only outline of the config
                # shape, not something anyone copies.
                continue
            # A complete config is validated elsewhere; a fragment is not, so
            # it has to carry the role itself or it teaches a broken pattern.
            if "role:" not in block:
                offenders.append(line)
        assert not offenders, (
            f"{path.name}: judges block(s) at line(s) {offenders} don't show `role:` on the "
            "judge's model — a reader copying this gets a config error"
        )


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

    def subcommands(self, command: str) -> list[str]:
        """A group's subcommands; empty for a plain command."""
        help_text = self.help_for(command)
        if "Commands:" not in help_text:
            return []
        section = help_text.split("Commands:")[1]
        return [line.split()[0] for line in section.splitlines() if line.strip()]

    def test_prose_never_names_a_command_that_does_not_exist(self):
        """The reverse of the check above, and the direction that misleads.

        An undocumented command is an omission a reader survives. An invented
        one is an instruction that fails when followed — and the plugin's
        markdown is followed by an agent, so it is scanned here too rather
        than trusted.
        """
        known = set(self.commands())
        groups = {name: self.subcommands(name) for name in known}
        pages = DOCS + sorted(REPO.glob("plugin/**/*.md"))
        wrong = []
        for path in pages:
            text = path.read_text(encoding="utf-8")
            for command, rest in re.findall(r"`evaling ([a-z][a-z-]*)([^`]*)`", text):
                if command not in known:
                    wrong.append(f"{path.name}: 'evaling {command}' is not a command")
                    continue
                # A group needs its subcommand: `evaling baseline set <id>`,
                # never `evaling baseline <id>`. A bare mention of the group is
                # fine — that is prose about the command, not an invocation.
                following = rest.split()
                if groups[command] and following and following[0] not in groups[command]:
                    wrong.append(
                        f"{path.name}: 'evaling {command} {following[0]}' — "
                        f"{command} takes {'|'.join(groups[command])}"
                    )
        assert not wrong, "prose names commands the CLI does not have: " + "; ".join(wrong)

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


class TestDocumentedInvocationsParse:
    """A documented command line must be one the CLI would actually accept.

    The two tests above check that commands and flags *exist*. They cannot
    catch a real command carrying a real flag in a position click rejects,
    which is how `evaling show <run> --case <id> -v` reached a release: `-v`
    is a global option and has to precede the subcommand.

    Scoped to misplaced and unknown options, which is that bug and the only
    thing this can judge honestly. A snippet naming a command without its
    arguments (`evaling compare`) is prose, not a broken invocation, and a
    placeholder standing in for a real value cannot be type-checked from here.

    Parsed, never run — `make_context` raises on a bad option without invoking
    the callback, so `cache clear` in the docs cannot clear anyone's cache.
    """

    PLACEHOLDER = re.compile(r"^(<[^>]+>|[A-Z][A-Z0-9_]*)$")

    #: Wrong on purpose: cli.md teaches global-flag placement by showing the
    #: form that fails. Guarded below, so it cannot outlive its counter-example.
    COUNTEREXAMPLES = {"evaling run --json"}

    def normalize(self, snippet: str) -> list[str]:
        """Turn a documented line into an argv a parser can judge.

        Placeholders become a dummy value, `[optional]` groups are taken as
        present (the stricter reading), and `a|b` takes its first alternative.
        """
        argv = []
        for token in snippet.replace("[", " ").replace("]", " ").split()[1:]:
            token = token.split("|", 1)[0]
            argv.append("x" if self.PLACEHOLDER.match(token) else token)
        return argv

    def parse(self, argv: list[str]) -> None:
        """Walk the group tree, parsing each level and running nothing."""
        import warnings

        import click

        command, context = main, main.make_context("evaling", list(argv))
        while isinstance(command, click.Group):
            with warnings.catch_warnings():
                # click 8 splits the subcommand off into `protected_args`;
                # click 9 drops it and leaves everything in `args`.
                warnings.simplefilter("ignore", DeprecationWarning)
                protected = list(getattr(context, "protected_args", []) or [])
            rest = protected + list(context.args)
            if not rest:
                break
            name, command, rest = command.resolve_command(context, rest)
            context = command.make_context(name, rest, parent=context)

    def invocations(self) -> list[tuple[Path, str]]:
        found = []
        for path in DOCS:
            for snippet in re.findall(r"`(evaling [^`]+)`", path.read_text(encoding="utf-8")):
                found.append((path, snippet))
        return found

    def test_there_are_invocations_to_check(self):
        """Guards the regex: a rename upstream would silently check nothing."""
        assert len(self.invocations()) > 20

    def test_each_counterexample_is_still_shown_as_one(self):
        """An exemption that outlives its reason hides the next real bug."""
        text = (REPO / "docs" / "cli.md").read_text(encoding="utf-8")
        for snippet in self.COUNTEREXAMPLES:
            assert f"not\n`{snippet}`" in text or f"not `{snippet}`" in text, (
                f"`{snippet}` is exempt as a counter-example but cli.md no longer "
                "presents it as the wrong form"
            )

    def test_every_documented_invocation_parses(self):
        import click

        broken = []
        for path, snippet in self.invocations():
            if snippet in self.COUNTEREXAMPLES:
                continue
            try:
                self.parse(self.normalize(snippet))
            except (click.NoSuchOption, click.BadOptionUsage) as err:
                broken.append(f"{path.name}: `{snippet}` — {err.format_message()}")
            except click.UsageError:
                # A missing argument or an unusable placeholder value: prose
                # naming a command, not a command line that would be refused.
                pass
            except (click.exceptions.Exit, SystemExit):
                # `--version` and `--help` are eager: they print and exit
                # during parsing, which means they parsed.
                pass
        assert not broken, "documented command lines the CLI would refuse:\n" + "\n".join(broken)


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
    """GitHub's anchor slugs for a file's headings.

    One hyphen per space, not one per run: a heading punctuated with an em dash
    drops the dash and leaves two spaces, so GitHub emits a double hyphen.
    Collapsing runs here made this checker generate a slug GitHub never does,
    and every link to such a heading passed while resolving nowhere.
    Underscores are kept, as GitHub keeps them.
    """
    slugs = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            continue
        title = line.lstrip("#").strip()
        slug = re.sub(r"[^\w\s-]", "", title.lower())
        slugs.add(re.sub(r"\s", "-", slug).strip("-"))
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


class TestMutationSandboxSeesWhatTestsRead:
    """mutmut runs the suite from a copy of the project, not the project.

    Anything a test reads has to be in `also_copy` or it is simply absent
    there. That does not skip the test — it fails the unmutated baseline, which
    aborts the run before a single mutant is tried, and the next scheduled run
    is the first anyone hears of it. Adding a test that reads a new directory
    is exactly when this breaks, so the check lives beside the tests.
    """

    def also_copy(self) -> set[str]:
        import tomllib

        config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        return set(config["tool"]["mutmut"]["also_copy"])

    def referenced(self) -> set[str]:
        """Top-level repo entries the test suite reaches for."""
        names = set()
        for path in sorted(REPO.glob("tests/*.py")):
            text = path.read_text(encoding="utf-8")
            names.update(re.findall(r'REPO / "([^"/]+)"', text))
            names.update(m.split("/")[0] for m in re.findall(r'REPO\.glob\("([^"]+)"', text))
        # This file states those patterns as literals, so scanning it matches
        # its own regexes. A real entry name has no metacharacters in it.
        names = {name for name in names if re.fullmatch(r"[A-Za-z0-9_.-]+", name)}
        # mutmut copies the source and the tests itself.
        return names - {"src", "tests", "pyproject.toml"}

    def test_every_directory_the_tests_read_is_copied(self):
        missing = sorted(self.referenced() - self.also_copy())
        assert not missing, (
            "these are read by tests but absent from [tool.mutmut] also_copy, "
            f"which aborts the mutation run: {missing}"
        )


class TestCommandProviderPayload:
    """The documented stdin shape, checked against the one actually written.

    Prose describing a wire format is the kind that rots invisibly: a script
    written against it fails at the first cell, long after the doc was read.
    """

    def documented_parts(self) -> list[dict]:
        """Every message part in the payload example in providers.md."""
        for _, block in code_blocks(REPO / "docs" / "providers.md", "json"):
            payload = json.loads(block)
            if isinstance(payload, dict) and "messages" in payload:
                return [part for message in payload["messages"] for part in message["parts"]]
        pytest.fail("providers.md has no command-provider payload example")

    def test_text_parts_carry_the_keys_the_example_shows(self):
        from evaling.render import RenderedMessage, RenderedText
        from evaling.storage import serialize_messages

        sent = serialize_messages([RenderedMessage("user", (RenderedText("hi"),))])
        documented = [part for part in self.documented_parts() if "text" in part]
        assert documented, "the example shows no text part"
        for part in documented:
            assert set(part) == set(sent[0]["parts"][0]), (
                "providers.md describes text-part keys the provider does not send"
            )

    def test_media_parts_carry_the_keys_the_example_shows(self, tmp_path):
        from evaling.content import MediaRef
        from evaling.render import RenderedMessage
        from evaling.storage import serialize_messages

        image = tmp_path / "a.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        media = MediaRef(kind="image", path=image, media_type="image/png", sha256="abc")
        sent = serialize_messages([RenderedMessage("user", (media,))])
        for part in self.documented_parts():
            if "text" in part:
                continue
            assert set(part) == set(sent[0]["parts"][0]), (
                "providers.md describes media-part keys the provider does not send"
            )


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
