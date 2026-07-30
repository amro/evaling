"""The CLI and the MCP server must agree about what they refuse.

Both are thin wrappers over the same core, so a guard that exists on one and
not the other is a bug in whichever is missing it — and that has now happened
five times: the large-matrix confirmation, `sample_seed` without `sample`, the
source-backed refusals, `--yes` against an unbounded source, and `list
--limit`. Each was found separately, by a person, after it shipped.

This is one scenario table driven through both surfaces. Adding a guard to
either without the other fails here, which is the point: the class stops
depending on someone noticing.

Deliberate differences are listed at the bottom, with the reason, so a reader
can tell "not yet implemented" from "decided".
"""

import asyncio

import pytest
from click.testing import CliRunner

from evaling.cli import main
from evaling.engine import CONFIRM_THRESHOLD
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
    ("an unbounded source", sourced(), {}, {}, False),
    ("an unbounded source, acknowledged", sourced(), {"yes": True}, {"confirm_large": True}, True),
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
    (
        "a large matrix, acknowledged",
        inline(CONFIRM_THRESHOLD + 5),
        {"yes": True},
        {"confirm_large": True},
        True,
    ),
    (
        "a large matrix with a cost ceiling",
        inline(CONFIRM_THRESHOLD + 5),
        {"max_cost": 5.0, "yes": True},
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


@pytest.mark.parametrize("scenario", [s for s in SCENARIOS if not s[4]], ids=lambda s: s[0])
def test_a_refusal_says_why_on_both(scenario, tmp_path):
    """A guard nobody can act on is a guard people work around."""
    name, config, cli_kwargs, mcp_kwargs, _ = scenario
    _, cli_message = via_cli(write(tmp_path / "cli", config), **cli_kwargs)
    _, mcp_message = via_mcp(write(tmp_path / "mcp", config), **mcp_kwargs)
    for surface, message in (("CLI", cli_message), ("MCP", mcp_message)):
        assert message.strip(), f"{name}: {surface} refused silently"
        assert len(message.split()) > 4, f"{name}: {surface} said only {message!r}"


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


class TestDeliberateDifferences:
    """Where the surfaces differ on purpose, so "missing" reads differently from "decided".

    Each of these is a decision, not an omission. If one starts failing, the
    decision changed and this file should say so.
    """

    def test_the_cli_prompts_where_mcp_refuses(self, tmp_path):
        """A large matrix at a terminal asks; an agent gets an error instead.

        A prompt needs someone to answer it. MCP has nobody, so the same
        ceiling has to be an error there — same threshold, different shape.
        """
        path = write(tmp_path, inline(CONFIRM_THRESHOLD + 5))
        # Not a tty under CliRunner, so the CLI proceeds; MCP refuses.
        assert via_cli(path)[0] is True
        accepted, message = via_mcp(write(tmp_path / "mcp", inline(CONFIRM_THRESHOLD + 5)))
        assert accepted is False
        assert "confirmation threshold" in message

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
