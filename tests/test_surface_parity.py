"""The CLI and the MCP server must agree about what they refuse.

Both are thin wrappers over the same core, so a guard that exists on one and
not the other is a bug in whichever is missing it — and that has now happened
five times: the large-matrix confirmation, `sample_seed` without `sample`, the
source-backed refusals, `--yes` against an unbounded source, and `list
--limit`. Each was found separately, by a person, after it shipped.

Three layers, because the first alone was not enough.

A scenario table drives both surfaces through the same refusals, and checks
they refuse for the same *reason* — agreeing that something is wrong while
disagreeing about what sends the reader to fix the wrong thing.

Every `run` option is classified as shared or CLI-only, so a new one on either
surface fails here until someone places it. The table covers the options it
happens to name, which is why five more gaps shipped after it was written:
`--fail-fast`, `--no-cache`, `--resume`, `--baseline` and `--no-look` were in
neither the table nor the list of deliberate differences.

The options both surfaces accept are then checked to *do* the same thing, not
just to be accepted by both.

Deliberate differences are listed with the reason, so a reader can tell "not
yet implemented" from "decided".
"""

import asyncio
import contextlib
from difflib import SequenceMatcher

import pytest
from click.testing import CliRunner

from evaling.cli import main
from evaling.errors import EvalingError
from evaling.mcp_server import run_eval_tool

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}

BASE = (
    "models: [{id: mock, provider: mock}]\n"
    "variants:\n  - name: v1\n"
    '    prompt: [{role: user, content: "{{ q }}"}]\n'
)
SCORECARD = 'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n'

#: A source that ends on its own after TOTAL cases. "Unbounded" in these
#: scenarios means the *config* sets no `limit` — which is what the guard
#: reacts to — not that the source runs forever, which would hang the suite.
SOURCE_TOTAL = 8

SOURCE = (
    "from evaling import Case, CasePage\n"
    f"TOTAL = {SOURCE_TOTAL}\n"
    "class S:\n"
    "    def fetch(self, cursor, limit):\n"
    "        start = int(cursor or 0)\n"
    "        stop = min(start + limit, TOTAL)\n"
    "        rows = [Case(id=f'c{i}', vars={'q': str(i)}) for i in range(start, stop)]\n"
    "        return CasePage(cases=rows, cursor=None if stop >= TOTAL else str(stop))\n"
    "def make():\n"
    "    return S()\n"
)


def inline(cases=2):
    ids = ", ".join(f"{{id: c{i}, vars: {{q: '{i}'}}}}" for i in range(cases))
    return BASE + f"cases: [{ids}]\n" + SCORECARD


def sourced(limit=None):
    bound = f", limit: {limit}" if limit is not None else ""
    return BASE + f"cases: {{source: 'src.py:make', page_size: 50{bound}}}\n" + SCORECARD


def write(tmp_path, config):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "src.py").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "eval.yaml").write_text(config, encoding="utf-8")
    return tmp_path


def via_cli(tmp_path, **kwargs):
    """Run through the CLI. Returns (accepted, message)."""
    args = ["run"]
    for name, value in kwargs.items():
        flag = "--" + name.replace("_", "-")
        if value is True:
            args.append(flag)
        elif value is not None:
            args += [flag, str(value)]
    result = CliRunner().invoke(
        main,
        ["-c", str(tmp_path / "eval.yaml"), "-o", str(tmp_path / "runs"), *args],
        env=ENV,
        catch_exceptions=False,
    )
    return result.exit_code == 0, result.output


def via_mcp(tmp_path, **kwargs):
    """Run through the MCP tool. Returns (accepted, message)."""
    try:
        asyncio.run(
            run_eval_tool(
                config_path=str(tmp_path / "eval.yaml"),
                output_dir=str(tmp_path / "runs"),
                **kwargs,
            )
        )
        return True, ""
    except EvalingError as exc:
        return False, str(exc)


#: (name, config, cli kwargs, mcp kwargs, should_be_accepted)
#:
#: The two kwarg sets differ only in spelling — `--max-cost` against
#: `max_cost_usd`, `--yes` against `confirm_large`. Where a scenario needs no
#: flags both are empty.
SCENARIOS = [
    ("a plain run", inline(), {}, {}, True),
    # Refused on both: nothing can interrupt an MCP run, and the CLI is not
    # at a tty under CliRunner, so both are the unwatched case.
    ("an unbounded source", sourced(), {}, {}, False),
    (
        "an unbounded source with a cost ceiling",
        sourced(),
        {"max_cost": 1.0},
        {"max_cost_usd": 1.0},
        True,
    ),
    ("a bounded source", sourced(limit=4), {}, {}, True),
    ("a source with a case filter", sourced(limit=4), {"case": "c1"}, {"cases": ["c1"]}, False),
    ("a source with a sample", sourced(limit=4), {"sample": 2}, {"sample": 2}, False),
    ("a seed with no sample", inline(), {"sample-seed": 7}, {"sample_seed": 7}, False),
    ("a zero sample", inline(), {"sample": 0}, {"sample": 0}, False),
    ("an unknown model", inline(), {"model": "ghost"}, {"models": ["ghost"]}, False),
    ("an unknown variant", inline(), {"variant": "ghost"}, {"variants": ["ghost"]}, False),
    ("an unknown case", inline(), {"case": "ghost"}, {"cases": ["ghost"]}, False),
    # A large matrix is no longer a decision point on either surface: the
    # size and its likely cost are reported, and the run proceeds.
    ("a large matrix", inline(150), {}, {}, True),
    (
        "a large matrix with a cost ceiling",
        inline(150),
        {"max_cost": 5.0},
        {"max_cost_usd": 5.0},
        True,
    ),
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s[0])
def test_both_surfaces_agree(scenario, tmp_path):
    name, config, cli_kwargs, mcp_kwargs, expected = scenario
    cli_ok, cli_message = via_cli(write(tmp_path / "cli", config), **cli_kwargs)
    mcp_ok, mcp_message = via_mcp(write(tmp_path / "mcp", config), **mcp_kwargs)

    assert cli_ok == expected, (
        f"{name}: CLI {'refused' if not cli_ok else 'accepted'}: {cli_message}"
    )
    assert mcp_ok == expected, (
        f"{name}: MCP {'refused' if not mcp_ok else 'accepted'}: {mcp_message}"
    )


#: Shortest run of explanation the two refusals must share, in characters.
#: Every scenario above clears this with margin — the tightest is "sample must
#: be at least 1" at 25 — so raising it further would only be measuring the
#: current wording rather than the property.
SHARED_REASON_CHARS = 20


def flat(text: str) -> str:
    """One line, single-spaced: both surfaces wrap, at different widths."""
    return " ".join(text.split())


@pytest.mark.parametrize("scenario", [s for s in SCENARIOS if not s[4]], ids=lambda s: s[0])
def test_a_refusal_says_why_on_both(scenario, tmp_path):
    """A guard nobody can act on is a guard people work around."""
    name, config, cli_kwargs, mcp_kwargs, _ = scenario
    _, cli_message = via_cli(write(tmp_path / "cli", config), **cli_kwargs)
    _, mcp_message = via_mcp(write(tmp_path / "mcp", config), **mcp_kwargs)
    for surface, message in (("CLI", cli_message), ("MCP", mcp_message)):
        assert message.strip(), f"{name}: {surface} refused silently"
        assert len(message.split()) > 4, f"{name}: {surface} said only {message!r}"


@pytest.mark.parametrize("scenario", [s for s in SCENARIOS if not s[4]], ids=lambda s: s[0])
def test_both_refusals_give_the_same_reason(scenario, tmp_path):
    """Refusing together is not enough — they have to refuse for one reason.

    Two surfaces can agree that something is wrong and disagree about what,
    which sends the reader to fix the wrong thing. Checked as the longest run
    of shared text rather than equality: each surface names its own argument
    (`--sample-seed` against `sample_seed`), which is correct, and the length
    bar alone was cleared by click's own boilerplate.
    """
    name, config, cli_kwargs, mcp_kwargs, _ = scenario
    _, cli_message = via_cli(write(tmp_path / "cli", config), **cli_kwargs)
    _, mcp_message = via_mcp(write(tmp_path / "mcp", config), **mcp_kwargs)

    cli_text, mcp_text = flat(cli_message), flat(mcp_message)
    match = SequenceMatcher(None, cli_text, mcp_text, autojunk=False).find_longest_match(
        0, len(cli_text), 0, len(mcp_text)
    )
    shared = mcp_text[match.b : match.b + match.size]
    assert match.size >= SHARED_REASON_CHARS, (
        f"{name}: the surfaces refused for different reasons.\n"
        f"  shared only {match.size} chars: {shared!r}\n"
        f"  CLI: {cli_text}\n"
        f"  MCP: {mcp_text}"
    )


class TestTheSameRunProducesTheSameNumbers:
    """Agreement about refusals is half of it; the results have to match too."""

    def test_counts_and_aggregates_are_identical(self, tmp_path):
        cli_path = write(tmp_path / "cli", inline(6))
        mcp_path = write(tmp_path / "mcp", inline(6))
        assert via_cli(cli_path)[0]
        summary = asyncio.run(
            run_eval_tool(
                config_path=str(mcp_path / "eval.yaml"), output_dir=str(mcp_path / "runs")
            )
        )

        from evaling.storage import RunStore

        cli_meta = RunStore(cli_path / "runs").load_meta(
            RunStore(cli_path / "runs").list_runs()[0]["id"]
        )
        assert cli_meta["counts"] == summary["counts"]
        assert cli_meta["aggregates"] == summary["aggregates"]

    def test_a_sampled_run_draws_the_same_cases_from_the_same_seed(self, tmp_path):
        from evaling.storage import RunStore

        cli_path = write(tmp_path / "cli", inline(20))
        mcp_path = write(tmp_path / "mcp", inline(20))
        via_cli(cli_path, sample=5, **{"sample-seed": 4242})
        asyncio.run(
            run_eval_tool(
                config_path=str(mcp_path / "eval.yaml"),
                output_dir=str(mcp_path / "runs"),
                sample=5,
                sample_seed=4242,
            )
        )
        picked = []
        for path in (cli_path, mcp_path):
            store = RunStore(path / "runs")
            run_id = store.list_runs()[0]["id"]
            picked.append(sorted(r.case_id for r in store.load_results(run_id)))
        assert picked[0] == picked[1], "the same seed drew different cases on the two surfaces"


class TestListingLimitsClampTheSameWay:
    def runs_shown(self, tmp_path, limit):
        from evaling.mcp_server import list_runs_tool

        write(tmp_path, inline(2))
        via_cli(tmp_path)
        via_cli(tmp_path)
        import json

        result = CliRunner().invoke(
            main,
            ["-o", str(tmp_path / "runs"), "--json", "list", "--limit", str(limit)],
            env=ENV,
            catch_exceptions=False,
        )
        from_cli = len(json.loads(result.output))
        from_mcp = len(list_runs_tool(limit=limit, output_dir=str(tmp_path / "runs"))["runs"])
        return from_cli, from_mcp

    @pytest.mark.parametrize("limit", [0, -1, 1, 5])
    def test_the_same_limit_shows_the_same_number(self, tmp_path, limit):
        from_cli, from_mcp = self.runs_shown(tmp_path, limit)
        assert from_cli == from_mcp, f"--limit {limit}: CLI showed {from_cli}, MCP {from_mcp}"


#: `evaling run` flag -> the `run_eval` argument that means the same thing.
SHARED_OPTIONS = {
    "--case": "cases",
    "--fail-fast": "fail_fast",
    "--label": "label",
    "--max-cost": "max_cost_usd",
    "--model": "models",
    "--no-cache": "no_cache",
    "--sample": "sample",
    "--sample-seed": "sample_seed",
    "--variant": "variants",
}

#: CLI-only, and why. "Not implemented" is a legitimate entry — the point is
#: that it is written down, not that every gap has a rationale.
CLI_ONLY_OPTIONS = {
    "--baseline": "no MCP equivalent yet; an agent can read the baseline with get_run",
    "--concurrency": "a settings knob, reachable through the config or EVALING_CONCURRENCY",
    "--dry-run": "render_prompt is the MCP way to check a config without spending",
    "--html": "writes a file to a caller-chosen path, which a tool call should not do",
    "--log-requests": "writes prompts and completions verbatim to a caller-chosen path",
    "--no-look": "privacy is a property of the config, which both surfaces already honor",
    "--resume": "no MCP equivalent yet",
}

#: Plumbing on the MCP tool: how it is called, not what it was asked to do.
MCP_PLUMBING = {"config_path", "output_dir", "on_progress"}


class TestEveryRunOptionIsClassified:
    """A new option on either surface fails here until someone places it.

    This is the guard the file's premise needs. The scenario table covers the
    options it happens to name; five gaps shipped anyway, and would have again
    — `--fail-fast`, `--no-cache`, `--resume`, `--baseline` and `--no-look`
    were in neither the table nor the deliberate-differences list, so adding
    one to a surface and not the other broke nothing here.
    """

    def cli_flags(self) -> set[str]:
        return {
            option
            for param in main.commands["run"].params
            for option in param.opts
            if option.startswith("--")
        }

    def mcp_arguments(self) -> set[str]:
        import inspect

        from evaling.mcp_server import run_eval_tool

        return set(inspect.signature(run_eval_tool).parameters) - MCP_PLUMBING

    def test_every_cli_flag_is_shared_or_explained(self):
        unclassified = self.cli_flags() - set(SHARED_OPTIONS) - set(CLI_ONLY_OPTIONS)
        assert not unclassified, (
            f"new `evaling run` flags: {sorted(unclassified)}. Add each to "
            "SHARED_OPTIONS with its MCP argument, or to CLI_ONLY_OPTIONS with the reason."
        )

    def test_every_mcp_argument_is_shared(self):
        unclassified = self.mcp_arguments() - set(SHARED_OPTIONS.values())
        assert not unclassified, (
            f"new run_eval arguments: {sorted(unclassified)}. Add each to SHARED_OPTIONS "
            "with its CLI flag, or to MCP_PLUMBING if it is not a user-facing option."
        )

    def test_the_classification_describes_options_that_exist(self):
        """An entry that outlives its option hides the next real gap."""
        flags = self.cli_flags()
        stale = (set(SHARED_OPTIONS) | set(CLI_ONLY_OPTIONS)) - flags
        assert not stale, f"classified flags that `evaling run` no longer has: {sorted(stale)}"
        gone = set(SHARED_OPTIONS.values()) - self.mcp_arguments()
        assert not gone, f"shared options whose MCP argument is gone: {sorted(gone)}"

    def test_each_cli_only_option_carries_a_reason(self):
        vague = [flag for flag, why in CLI_ONLY_OPTIONS.items() if len(why.split()) < 4]
        assert not vague, f"these need a real reason, not a placeholder: {vague}"


class TestSharedOptionsBehaveTheSame:
    """The options both surfaces accept must also *do* the same thing.

    `--max-cost` was in the scenario table only as a permission token — as the
    thing that lets an unbounded run start. Whether the two surfaces stop at
    the same point, and agree the run is incomplete, was untested.
    """

    def results(self, path):
        from evaling.storage import RunStore

        store = RunStore(path / "runs")
        run_id = store.list_runs()[0]["id"]
        return store.load_meta(run_id)

    def both(self, tmp_path, config, cli_kwargs, mcp_kwargs):
        cli_path = write(tmp_path / "cli", config)
        mcp_path = write(tmp_path / "mcp", config)
        via_cli(cli_path, **cli_kwargs)
        # A refusal is still a result worth comparing, and the CLI swallows
        # its own into an exit code rather than raising.
        with contextlib.suppress(EvalingError):
            asyncio.run(
                run_eval_tool(
                    config_path=str(mcp_path / "eval.yaml"),
                    output_dir=str(mcp_path / "runs"),
                    **mcp_kwargs,
                )
            )
        return self.results(cli_path), self.results(mcp_path)

    def test_fail_fast_stops_both_at_the_same_place(self, tmp_path):
        # Every cell fails, so fail-fast has something to stop on.
        config = (
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            + "cases: ["
            + ", ".join(f"{{id: c{i}, vars: {{q: '{i}'}}}}" for i in range(6))
            + "]\n"
            + 'scorecard: [{criterion: acc, scorer: {type: contains, value: "nowhere"}}]\n'
            + "settings: {concurrency: 1}\n"
        )
        cli_meta, mcp_meta = self.both(tmp_path, config, {"fail-fast": True}, {"fail_fast": True})
        assert cli_meta["counts"] == mcp_meta["counts"], (
            f"--fail-fast stopped at {cli_meta['counts']} on the CLI and "
            f"{mcp_meta['counts']} over MCP"
        )
        assert cli_meta["stopped_early"] == mcp_meta["stopped_early"]
        assert cli_meta["counts"]["total"] < 6, "fail-fast did not stop anything"
        assert cli_meta["stopped_early"] is True

    def test_max_cost_stops_both_at_the_same_place(self, tmp_path):
        config = (
            "models: [{id: mock, provider: mock, params: {cost: 0.01}}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            + "cases: ["
            + ", ".join(f"{{id: c{i}, vars: {{q: '{i}'}}}}" for i in range(10))
            + "]\n"
            + 'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n'
            + "settings: {concurrency: 1}\n"
        )
        cli_meta, mcp_meta = self.both(tmp_path, config, {"max-cost": 0.03}, {"max_cost_usd": 0.03})
        assert cli_meta["counts"] == mcp_meta["counts"]
        assert cli_meta["totals"]["cost_usd"] == mcp_meta["totals"]["cost_usd"]
        assert cli_meta["status"] == mcp_meta["status"]
        assert cli_meta["warnings"] == mcp_meta["warnings"]
        assert cli_meta["counts"]["total"] < 10, "the ceiling stopped nothing"
        # A ceiling-stopped run is `incomplete` and resumable; `stopped_early`
        # is --fail-fast's flag, and must stay off here.
        assert cli_meta["status"] == "incomplete"
        assert cli_meta["stopped_early"] is False
        assert cli_meta["totals"]["cost_usd"] <= 0.03

    def test_no_cache_bypasses_the_cache_on_both(self, tmp_path):
        config = inline(4) + "settings: {cache: true}\n"
        for path in (tmp_path / "cli", tmp_path / "mcp"):
            write(path, config)
        # Prime each surface's own cache, then ask it to ignore it.
        via_cli(tmp_path / "cli")
        asyncio.run(
            run_eval_tool(
                config_path=str(tmp_path / "mcp" / "eval.yaml"),
                output_dir=str(tmp_path / "mcp" / "runs"),
            )
        )
        via_cli(tmp_path / "cli", **{"no-cache": True})
        mcp_summary = asyncio.run(
            run_eval_tool(
                config_path=str(tmp_path / "mcp" / "eval.yaml"),
                output_dir=str(tmp_path / "mcp" / "runs"),
                no_cache=True,
            )
        )
        cli_meta = self.results(tmp_path / "cli")
        assert cli_meta["counts"]["cached"] == 0, "--no-cache still served from the cache"
        assert mcp_summary["counts"]["cached"] == 0, "no_cache still served from the cache"

    def test_a_label_lands_the_same_way_on_both(self, tmp_path):
        cli_path = write(tmp_path / "cli", inline(2))
        mcp_path = write(tmp_path / "mcp", inline(2))
        via_cli(cli_path, label="tagged")
        asyncio.run(
            run_eval_tool(
                config_path=str(mcp_path / "eval.yaml"),
                output_dir=str(mcp_path / "runs"),
                label="tagged",
            )
        )
        assert self.results(cli_path)["label"] == self.results(mcp_path)["label"] == "tagged"


class TestDeliberateDifferences:
    """Where the surfaces differ on purpose, so "missing" reads differently from "decided".

    Each of these is a decision, not an omission. If one starts failing, the
    decision changed and this file should say so.
    """

    def test_an_unbounded_source_is_refused_on_both(self, tmp_path):
        """The one guard left, and both surfaces keep it.

        Neither can be interrupted here — CliRunner has no tty, MCP has no
        Ctrl-C — and the size is unknown even to whoever wrote the config,
        since it depends on what the source returns.
        """
        path = write(tmp_path, sourced())
        assert via_cli(path)[0] is False
        accepted, message = via_mcp(write(tmp_path / "mcp", sourced()))
        assert accepted is False
        assert "whatever the source returns" in message

    def test_render_prompt_has_no_cli_equivalent(self, tmp_path):
        """`validate` renders everything; `render_prompt` renders one case.

        They are different tools, so no-look treats them differently: validate
        hashes ids and withholds errors, render_prompt is refused outright.
        """
        from evaling.mcp_server import render_prompt_tool

        path = write(tmp_path, inline(2) + "privacy: {no_look: true}\n")
        with pytest.raises(EvalingError, match="cannot show a no-look config"):
            render_prompt_tool(config_path=str(path / "eval.yaml"))
        # The CLI's nearest equivalent runs, with the data withheld.
        assert via_cli(path, **{"dry-run": True})[0] is True
