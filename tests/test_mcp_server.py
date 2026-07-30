"""MCP tools, tested directly (the transport is the SDK's job, not ours)."""

import asyncio
import contextlib
import json
import os
from datetime import timedelta

import pytest
import yaml
from click.testing import CliRunner

from evaling.cli import main
from evaling.config import Settings, load_config
from evaling.engine import run_eval, run_eval_async
from evaling.errors import EvalingError
from evaling.mcp_server import (
    PAGE_SIZE,
    build_server,
    compare_runs_tool,
    get_case_result_tool,
    get_run_tool,
    list_runs_tool,
    render_prompt_tool,
    run_eval_tool,
    set_baseline_tool,
)
from evaling.providers import _REGISTRY
from evaling.providers.mock import MockProvider
from evaling.storage import RunStore
from helpers import make_config, make_settings

CONFIG = """\
models: [{id: mock, provider: mock}]
variants:
  - name: v1
    prompt: [{role: user, content: "{{ q }}"}]
cases:
  - {id: good, vars: {q: alpha}, expected: alpha}
  - {id: bad, vars: {q: beta}, expected: NOPE}
scorecard: [{criterion: acc, scorer: {type: exact}}]
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "eval.yaml").write_text(CONFIG, encoding="utf-8")
    return tmp_path


def runs_dir(tmp_path):
    return str(tmp_path / "runs")


class TestRunEval:
    def test_returns_summary_not_records(self, project):
        result = asyncio.run(
            run_eval_tool(config_path=str(project / "eval.yaml"), output_dir=runs_dir(project))
        )
        assert result["counts"]["total"] == 2
        assert result["aggregates"]["overall"]["pass_rate"] == 0.5
        # token-frugal: no full record dump
        assert "records" not in result and "cells" not in result
        assert result["failure_count"] == 1
        assert result["first_failures"][0]["case_id"] == "bad"
        assert "get_run" in result["hint"]

    def test_progress_callback_fires_per_cell(self, project):
        seen = []
        asyncio.run(
            run_eval_tool(
                config_path=str(project / "eval.yaml"),
                output_dir=runs_dir(project),
                on_progress=lambda done, total: seen.append((done, total)),
            )
        )
        assert seen == [(1, 2), (2, 2)]

    def test_filters_narrow_the_matrix(self, project):
        result = asyncio.run(
            run_eval_tool(
                config_path=str(project / "eval.yaml"),
                cases=["good"],
                output_dir=runs_dir(project),
            )
        )
        assert result["counts"]["total"] == 1
        assert result.get("failure_count") is None

    def test_runs_inside_an_event_loop(self, project):
        # The MCP server calls this from a running loop; run_eval() would
        # raise there, so the tool must use run_eval_async.
        async def go():
            return await run_eval_tool(
                config_path=str(project / "eval.yaml"), output_dir=runs_dir(project)
            )

        assert asyncio.run(go())["counts"]["total"] == 2


class TestGetRun:
    def finished(self, project):
        asyncio.run(
            run_eval_tool(
                config_path=str(project / "eval.yaml"),
                label="mcp-test",
                output_dir=runs_dir(project),
            )
        )
        return runs_dir(project)

    def test_summary_is_compact(self, project):
        out = get_run_tool("latest", output_dir=self.finished(project))
        assert out["label"] == "mcp-test"
        assert out["aggregates"]["overall"]["cases"] == 2
        assert "cells" not in out

    def test_failures_detail(self, project):
        out = get_run_tool("latest", detail="failures", output_dir=self.finished(project))
        assert [cell["case_id"] for cell in out["cells"]] == ["bad"]
        assert out["cells"][0]["failed_criteria"]["acc"] == "expected 'NOPE'"
        assert out["page"]["total"] == 1

    def test_full_detail_paginates(self, project):
        out = get_run_tool("latest", detail="full", output_dir=self.finished(project))
        assert out["page"] == {
            "page": 1,
            "page_size": PAGE_SIZE,
            "returned": 2,
            "total": 2,
            "has_more": False,
        }

    def test_pagination_windows(self, tmp_path):
        cases = [{"id": f"c{i}", "vars": {"q": str(i)}} for i in range(25)]
        config = make_config(tmp_path, cases=cases)
        settings = make_settings(tmp_path)
        result = run_eval(config, settings)

        page1 = get_run_tool(result.run_id, detail="full", output_dir=str(settings.output_dir))
        assert page1["page"]["returned"] == PAGE_SIZE
        assert page1["page"]["has_more"] is True
        page2 = get_run_tool(
            result.run_id, detail="full", page=2, output_dir=str(settings.output_dir)
        )
        assert page2["page"]["returned"] == 5
        assert page2["page"]["has_more"] is False

    def test_long_output_is_snipped_with_a_pointer(self, tmp_path):
        long_text = "x" * 5000
        config = make_config(
            tmp_path,
            models=[{"id": "m1", "provider": "mock", "params": {"response": long_text}}],
            cases=[{"id": "c1", "vars": {"q": "x"}, "expected": "nope"}],
        )
        settings = make_settings(tmp_path)
        result = run_eval(config, settings)
        out = get_run_tool(result.run_id, detail="full", output_dir=str(settings.output_dir))
        output = out["cells"][0]["output"]
        assert len(output) < 1000
        assert "get_case_result" in output

    def test_bad_detail_rejected(self, project):
        with pytest.raises(EvalingError, match="unknown detail"):
            get_run_tool("latest", detail="everything", output_dir=self.finished(project))

    def test_bad_page_rejected(self, project):
        with pytest.raises(EvalingError, match="page must be"):
            get_run_tool("latest", detail="full", page=0, output_dir=self.finished(project))


class TestGetCaseResult:
    def test_full_untruncated_cell(self, tmp_path):
        long_text = "y" * 3000
        config = make_config(
            tmp_path,
            models=[{"id": "m1", "provider": "mock", "params": {"response": long_text}}],
            cases=[{"id": "c1", "vars": {"q": "x"}, "expected": long_text}],
        )
        settings = make_settings(tmp_path)
        result = run_eval(config, settings)
        cell = get_case_result_tool(
            result.run_id, "v1", "m1", "c1", output_dir=str(settings.output_dir)
        )
        assert cell["output"] == long_text  # not snipped
        assert cell["scores"]["acc"]["passed"] is True
        assert cell["messages"][0]["role"] == "user"
        assert "cost_usd" in cell["usage"]

    def test_unknown_cell_names_the_escape_hatch(self, project):
        asyncio.run(
            run_eval_tool(config_path=str(project / "eval.yaml"), output_dir=runs_dir(project))
        )
        with pytest.raises(EvalingError, match="no cell.*get_run"):
            get_case_result_tool("latest", "ghost", "mock", "good", output_dir=runs_dir(project))


class TestOtherTools:
    def test_list_runs_and_baseline(self, project):
        out_dir = runs_dir(project)
        asyncio.run(
            run_eval_tool(config_path=str(project / "eval.yaml"), label="first", output_dir=out_dir)
        )
        asyncio.run(
            run_eval_tool(
                config_path=str(project / "eval.yaml"), label="second", output_dir=out_dir
            )
        )

        listing = list_runs_tool(output_dir=out_dir)
        assert listing["total"] == 2
        assert listing["runs"][0]["label"] == "second"  # newest first
        assert listing["baseline"] is None

        pinned = set_baseline_tool("latest", output_dir=out_dir)
        assert list_runs_tool(output_dir=out_dir)["baseline"] == pinned["baseline"]

    def test_list_runs_limit(self, project):
        out_dir = runs_dir(project)
        for _ in range(3):
            asyncio.run(run_eval_tool(config_path=str(project / "eval.yaml"), output_dir=out_dir))
        assert len(list_runs_tool(limit=2, output_dir=out_dir)["runs"]) == 2

    def test_compare_runs(self, tmp_path):
        settings = make_settings(tmp_path)
        good = make_config(tmp_path, cases=[{"id": "c1", "vars": {"q": "a"}, "expected": "a"}])
        worse = make_config(tmp_path, cases=[{"id": "c1", "vars": {"q": "a"}, "expected": "X"}])
        a = run_eval(good, settings)
        b = run_eval(worse, settings)
        diff = compare_runs_tool(a.run_id, b.run_id, output_dir=str(settings.output_dir))
        assert diff["overall"]["score_delta"] == -1.0
        assert diff["cells"][0]["pass_rate_delta"] == -1.0

    def test_render_prompt_single_case(self, project):
        out = render_prompt_tool(str(project / "eval.yaml"), variant="v1", case_id="good")
        assert out["messages"][0]["parts"][0]["text"] == "alpha"
        # no run was created — nothing was called
        assert not (project / "runs").exists()

    def test_render_prompt_validates_whole_config(self, project):
        out = render_prompt_tool(str(project / "eval.yaml"))
        assert out == {"requests": 2, "render_errors": [], "ok": True}

    def test_render_prompt_reports_template_errors(self, tmp_path):
        (tmp_path / "bad.yaml").write_text(
            CONFIG.replace("{{ q }}", "{{ missing }}"), encoding="utf-8"
        )
        out = render_prompt_tool(str(tmp_path / "bad.yaml"))
        assert out["ok"] is False
        assert "'missing' is undefined" in out["render_errors"][0]["error"]


class TestServerWiring:
    def test_all_required_tools_registered(self, tmp_path):
        server = build_server(output_dir=str(tmp_path))
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        assert names == {
            "run_eval",
            "get_run",
            "get_case_result",
            "compare_runs",
            "list_runs",
            "set_baseline",
            "render_prompt",
        }

    def test_tools_have_descriptions(self, tmp_path):
        server = build_server(output_dir=str(tmp_path))
        for tool in asyncio.run(server.list_tools()):
            assert tool.description, f"{tool.name} needs a description for the agent"

    def test_call_through_the_server(self, project):
        server = build_server(output_dir=runs_dir(project))

        async def go():
            await server.call_tool("run_eval", {"config_path": str(project / "eval.yaml")})
            return await server.call_tool("list_runs", {})

        result = asyncio.run(go())
        # FastMCP returns (content_blocks, structured_result)
        structured = result[1] if isinstance(result, tuple) else result
        assert structured["total"] == 1


@pytest.mark.slow
class TestOverTheProtocol:
    """Drive the server the way an agent does — stdio, JSON-RPC, real subprocess.

    Everything above calls the tool functions directly, so the protocol layer
    was entirely untested: registration, schema generation, serialization, and
    error mapping. Two bugs lived there — the server reported the MCP SDK's
    version as its own, and an unknown argument was silently dropped.
    """

    @pytest.fixture
    def project(self, tmp_path):
        (tmp_path / "eval.yaml").write_text(
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: [{id: c1, vars: {q: hi}}, {id: c2, vars: {q: there}}]\n"
            "scorecard: [{criterion: acc, scorer: {type: contains, value: ''}}]\n",
            encoding="utf-8",
        )
        return tmp_path

    def drive(self, project, body):
        """Run `body(session)` against a real `evaling mcp` subprocess."""
        import sys

        async def go():
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "evaling", "mcp"],
                cwd=str(project),
            )
            async with (
                stdio_client(params) as (read, write),
                # A server that starts but never answers — a stray print() to
                # stdout corrupting the framing, say — would otherwise hang the
                # CI job to its limit instead of failing.
                ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session,
            ):
                init = await session.initialize()
                return await body(session, init)

        return asyncio.run(go())

    def test_server_identifies_itself_as_evaling(self, project):
        from evaling import __version__

        async def body(session, init):
            return init.serverInfo.name, init.serverInfo.version

        name, version = self.drive(project, body)
        assert name == "evaling"
        assert version == __version__, "the server reported the MCP SDK's version, not its own"

    def test_every_tool_is_reachable(self, project):
        async def body(session, init):
            return sorted(t.name for t in (await session.list_tools()).tools)

        assert self.drive(project, body) == [
            "compare_runs",
            "get_case_result",
            "get_run",
            "list_runs",
            "render_prompt",
            "run_eval",
            "set_baseline",
        ]

    def test_a_run_completes_through_the_protocol(self, project):
        async def body(session, init):
            result = await session.call_tool("run_eval", {})
            return json.loads(result.content[0].text)

        summary = self.drive(project, body)
        assert summary["counts"] == {"total": 2, "succeeded": 2, "failed": 0, "cached": 0}
        assert summary["aggregates"]["overall"]["cases"] == 2

    def test_errors_reach_the_client_as_errors(self, project):
        async def body(session, init):
            result = await session.call_tool("get_run", {"run_id": "ghost"})
            return result.isError, result.content[0].text

        is_error, text = self.drive(project, body)
        assert is_error is True
        assert "no run matches 'ghost'" in text

    def test_tools_advertise_that_they_take_no_extra_arguments(self, project):
        """A misspelled argument must not read as a successful run of the default.

        This is advertisement, not enforcement — see _forbid_unknown_arguments.
        A client that validates against the schema refuses before we see it.
        """

        async def body(session, init):
            return {
                t.name: t.inputSchema.get("additionalProperties")
                for t in (await session.list_tools()).tools
            }

        strictness = self.drive(project, body)
        permissive = [name for name, value in strictness.items() if value is not False]
        assert not permissive, f"these tools accept unknown arguments: {permissive}"

    def test_an_unknown_argument_is_refused(self, project):
        """A misspelled argument must not read as a successful default run."""

        async def body(session, init):
            result = await session.call_tool("run_eval", {"config": "somewhere-else.yaml"})
            return result.isError, result.content[0].text

        is_error, text = self.drive(project, body)
        assert is_error is True, "an unknown argument was accepted and dropped"
        assert "config" in text


class TestProgressNotifications:
    """The fire-and-forget progress path, which had no coverage at any layer.

    In-process rather than a subprocess: `create_connected_server_and_client_session`
    runs the whole JSON-RPC layer over memory streams, so this is fast enough
    not to need the slow marker.
    """

    def collect(self, tmp_path, cases=3):
        config = make_config(
            tmp_path, cases=[{"id": f"c{i}", "vars": {"q": str(i)}} for i in range(cases)]
        )
        (tmp_path / "eval.yaml").write_text(
            yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8"
        )
        seen = []

        async def go():
            from mcp.shared.memory import create_connected_server_and_client_session as connect

            server = build_server(output_dir=str(tmp_path / "runs"), config_path=None)

            async def on_progress(progress, total, message):
                seen.append((progress, total, message))

            async with connect(server._mcp_server) as session:
                result = await session.call_tool(
                    "run_eval",
                    {"config_path": str(tmp_path / "eval.yaml")},
                    progress_callback=on_progress,
                )
                return json.loads(result.content[0].text)

        return asyncio.run(go()), seen

    def test_a_notification_arrives_for_every_cell(self, tmp_path):
        summary, seen = self.collect(tmp_path)
        assert summary["counts"]["total"] == 3
        assert len(seen) == 3, f"expected one notification per cell, got {seen}"
        assert [p for p, _, _ in seen] == [1.0, 2.0, 3.0]
        assert all(total == 3.0 for _, total, _ in seen)
        assert seen[-1][2] == "3/3 cells"

    def test_a_run_without_a_listener_still_completes(self, tmp_path):
        """Progress is fire-and-forget: no client callback must not fail a run."""

        async def go():
            from mcp.shared.memory import create_connected_server_and_client_session as connect

            config = make_config(tmp_path, cases=[{"id": "c1", "vars": {"q": "x"}}])
            (tmp_path / "eval.yaml").write_text(
                yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8"
            )
            server = build_server(output_dir=str(tmp_path / "runs"), config_path=None)
            async with connect(server._mcp_server) as session:
                result = await session.call_tool(
                    "run_eval", {"config_path": str(tmp_path / "eval.yaml")}
                )
                return json.loads(result.content[0].text)

        assert asyncio.run(go())["counts"]["succeeded"] == 1


class TestSourceBackedRunsOverMcp:
    """A source-backed config was completely unrunnable over MCP.

    `run_eval_tool` called `select_matrix` purely to compute a progress total,
    and that raises for a source. The error even said "use run_eval" to someone
    already calling run_eval. Found by driving the no-look example for real —
    no test reached it, because every MCP test used inline cases.
    """

    def project(self, tmp_path, limit="limit: 4"):
        (tmp_path / "src.py").write_text(
            "from evaling import Case, CasePage\n"
            "class S:\n"
            "    def fetch(self, cursor, limit):\n"
            "        start = int(cursor or 0)\n"
            "        stop = min(start + limit, 10)\n"
            "        cases = [Case(id=f'c{i}', vars={'q': str(i)}) for i in range(start, stop)]\n"
            "        return CasePage(cases=cases, cursor=str(stop) if stop < 10 else None)\n"
            "def make():\n    return S()\n",
            encoding="utf-8",
        )
        (tmp_path / "eval.yaml").write_text(
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            f"cases: {{source: 'src.py:make', page_size: 2, {limit}}}\n"
            "scorecard: [{criterion: acc, scorer: {type: contains, value: ''}}]\n",
            encoding="utf-8",
        )
        return tmp_path

    def call(self, project, args=None):
        async def go():
            from mcp.shared.memory import create_connected_server_and_client_session as connect

            server = build_server(output_dir=str(project / "runs"), config_path=None)
            async with connect(server._mcp_server) as session:
                return await session.call_tool(
                    "run_eval", {"config_path": str(project / "eval.yaml"), **(args or {})}
                )

        return asyncio.run(go())

    def test_a_source_backed_config_runs(self, tmp_path):
        result = self.call(self.project(tmp_path))
        assert not result.isError, result.content[0].text
        assert json.loads(result.content[0].text)["counts"]["total"] == 4

    def test_an_unbounded_source_asks_for_a_ceiling(self, tmp_path):
        """An agent should not be able to start an unbounded paid run by accident."""
        result = self.call(self.project(tmp_path, limit="page_size: 2"))
        assert result.isError
        assert "limit" in result.content[0].text and "max_cost_usd" in result.content[0].text

    def test_an_unbounded_source_runs_with_a_ceiling(self, tmp_path):
        result = self.call(self.project(tmp_path, limit="page_size: 2"), {"max_cost_usd": 1.0})
        assert not result.isError, result.content[0].text
        assert json.loads(result.content[0].text)["counts"]["total"] == 10

    def test_lenient_argument_encodings_still_work(self, tmp_path):
        """Some clients send list and object arguments as JSON-encoded strings.

        FastMCP has `pre_parse_json` for exactly that. Full jsonschema
        validation would run ahead of it and refuse calls those clients make
        correctly, so the unknown-argument check looks at names only.
        """
        project = self.project(tmp_path)
        result = self.call(project, {"variants": '["v1"]'})
        assert not result.isError, result.content[0].text
        assert json.loads(result.content[0].text)["counts"]["total"] == 4

    def test_an_unknown_argument_names_what_the_tool_takes(self, tmp_path):
        result = self.call(self.project(tmp_path), {"varients": ["v1"]})
        assert result.isError
        text = result.content[0].text
        assert "varients" in text and "This tool takes:" in text and "variants" in text

    def test_case_filters_are_refused_with_a_reason(self, tmp_path):
        result = self.call(self.project(tmp_path), {"cases": ["c1"]})
        assert result.isError
        assert "fetched lazily" in result.content[0].text


class TestNoLookOverASourceOverMcp:
    """The combination all three features are actually used in together.

    Each was exercised separately — no-look by hand, sources in the tests
    above — but the intersection had no coverage, and it is the configuration
    the feature exists for: production rows an agent may run against but not
    read.
    """

    CANARY = "PATIENT-Rk8x2-MCP-4417"

    def project(self, tmp_path):
        (tmp_path / "src.py").write_text(
            "from evaling import Case, CasePage\n"
            f"MARK = {self.CANARY!r}\n"
            "def make():\n"
            "    class S:\n"
            "        def fetch(self, cursor, limit):\n"
            "            start = int(cursor or 0); stop = min(start + limit, 6)\n"
            "            cases = [Case(id=f'{MARK}-{i}', vars={'note': f'{MARK} {i}'},\n"
            "                          expected=MARK) for i in range(start, stop)]\n"
            "            return CasePage(cases=cases, cursor=str(stop) if stop < 6 else None)\n"
            "    return S()\n",
            encoding="utf-8",
        )
        (tmp_path / "eval.yaml").write_text(
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ note }}"}]\n'
            "cases: {source: 'src.py:make', page_size: 2, limit: 6}\n"
            # A failing criterion, whose detail names the expected value.
            "scorecard: [{criterion: acc, scorer: {type: exact}}]\n"
            "privacy: {no_look: true}\n",
            encoding="utf-8",
        )
        return tmp_path

    def test_nothing_leaks_through_any_tool(self, tmp_path):
        project = self.project(tmp_path)

        async def go():
            from mcp.shared.memory import create_connected_server_and_client_session as connect

            server = build_server(output_dir=str(project / "runs"), config_path=None)
            seen = []
            async with connect(server._mcp_server) as session:
                run = await session.call_tool(
                    "run_eval", {"config_path": str(project / "eval.yaml")}
                )
                seen.append(run.content[0].text)
                for detail in ("summary", "failures", "full"):
                    res = await session.call_tool("get_run", {"run_id": "latest", "detail": detail})
                    seen.append(res.content[0].text)
                cells = json.loads(seen[-1]).get("cells") or []
                if cells:
                    c = cells[0]
                    cell = await session.call_tool(
                        "get_case_result",
                        {
                            "run_id": "latest",
                            "variant": c["variant"],
                            "model": c["model"],
                            "case_id": c["case_id"],
                        },
                    )
                    seen.append(cell.content[0].text)
            return json.loads(seen[0]), "".join(seen)

        summary, everything = asyncio.run(go())
        assert summary["counts"]["total"] == 6, summary
        assert self.CANARY not in everything, "case data reached an MCP client"
        # and nothing on disk either
        for path in (project / "runs").rglob("*"):
            if path.is_file():
                assert self.CANARY not in path.read_text(encoding="utf-8", errors="ignore")


class TestConcurrentToolCalls:
    """One server instance, two runs in flight, one output directory.

    Every test above issues one call at a time, but an agent driving two
    variants in parallel — or two agents on one server — is ordinary. The
    things that can go wrong are shared state in the server, results from one
    run landing in the other's directory, and progress notifications
    addressed to the wrong caller.
    """

    def config(self, cases):
        return (
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: ["
            + ", ".join(f"{{id: c{i}, vars: {{q: '{i}'}}}}" for i in range(cases))
            + "]\n"
            "scorecard: [{criterion: acc, scorer: {type: contains, value: ''}}]\n"
        )

    def drive(self, tmp_path, sizes):
        """Run one `run_eval` per size, concurrently, on a single server."""
        for index, size in enumerate(sizes):
            (tmp_path / f"eval{index}.yaml").write_text(self.config(size), encoding="utf-8")
        progress = {index: [] for index in range(len(sizes))}

        async def go():
            from mcp.shared.memory import create_connected_server_and_client_session as connect

            server = build_server(output_dir=str(tmp_path / "runs"), config_path=None)

            async with connect(server._mcp_server) as session:

                async def call(index):
                    async def on_progress(done, total, message):
                        progress[index].append((done, total))

                    result = await session.call_tool(
                        "run_eval",
                        {
                            "config_path": str(tmp_path / f"eval{index}.yaml"),
                            "label": f"run-{index}",
                        },
                        progress_callback=on_progress,
                    )
                    return json.loads(result.content[0].text)

                return await asyncio.gather(*(call(i) for i in range(len(sizes))))

        return asyncio.run(go()), progress

    def test_both_runs_complete_and_stay_separate(self, tmp_path):
        sizes = [3, 7]
        summaries, _ = self.drive(tmp_path, sizes)

        assert [s["counts"]["total"] for s in summaries] == sizes
        assert summaries[0]["run_id"] != summaries[1]["run_id"]

        # Each run's own directory holds exactly its own cells, so nothing
        # crossed over on the way to disk.
        store = RunStore(tmp_path / "runs")
        for summary, size in zip(summaries, sizes, strict=True):
            records = store.load_results(summary["run_id"])
            assert len(records) == size
            assert {r.case_id for r in records} == {f"c{i}" for i in range(size)}

    def test_progress_notifications_do_not_cross(self, tmp_path):
        """Each caller's token must only ever carry that caller's totals."""
        sizes = [3, 7]
        _, progress = self.drive(tmp_path, sizes)

        for index, size in enumerate(sizes):
            reported = progress[index]
            assert len(reported) == size, f"run {index} got {len(reported)} notifications"
            assert {total for _, total in reported} == {float(size)}
            assert [done for done, _ in reported] == [float(n) for n in range(1, size + 1)]

    def test_one_run_failing_does_not_take_the_other_down(self, tmp_path):
        """A bad config in one call must not disturb a healthy concurrent one."""
        (tmp_path / "good.yaml").write_text(self.config(3), encoding="utf-8")

        async def go():
            from mcp.shared.memory import create_connected_server_and_client_session as connect

            server = build_server(output_dir=str(tmp_path / "runs"), config_path=None)
            async with connect(server._mcp_server) as session:
                good, bad = await asyncio.gather(
                    session.call_tool("run_eval", {"config_path": str(tmp_path / "good.yaml")}),
                    session.call_tool("run_eval", {"config_path": str(tmp_path / "missing.yaml")}),
                )
                return json.loads(good.content[0].text), bad

        summary, failure = asyncio.run(go())
        assert summary["counts"]["total"] == 3
        assert failure.isError is True


class TestInterruptedRun:
    """What the run directory looks like when a run is cut off mid-flight.

    A blocking `run_eval` can be interrupted by the client going away, the
    agent cancelling, or the transport dropping. Nothing covered what survives
    on disk or whether `--resume` could pick it up — which matters most for
    exactly the runs worth resuming, the long expensive ones.
    """

    #: Cells that complete before the rest hang, so the interruption always
    #: lands in the middle of a run rather than at a random point in it.
    COMPLETED = 4

    @pytest.fixture
    def stalling_mock(self, monkeypatch):
        """A provider that answers COMPLETED calls, then hangs until released."""
        state = {"calls": 0, "hang": True, "reached": asyncio.Event()}

        class Stalling(MockProvider):
            async def complete(self, request):
                state["calls"] += 1
                if state["hang"] and state["calls"] > TestInterruptedRun.COMPLETED:
                    state["reached"].set()
                    await asyncio.sleep(3600)
                return await super().complete(request)

        monkeypatch.setitem(_REGISTRY, "mock", Stalling)
        return state

    def config_text(self, cells=20):
        return (
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: ["
            + ", ".join(f"{{id: c{i}, vars: {{q: '{i}'}}}}" for i in range(cells))
            + "]\n"
            "scorecard: [{criterion: acc, scorer: {type: contains, value: ''}}]\n"
        )

    def project(self, tmp_path, cells=20):
        (tmp_path / "eval.yaml").write_text(self.config_text(cells), encoding="utf-8")
        return tmp_path

    def interrupt(self, project, state, concurrency=2):
        """Start a run, wait until it is genuinely mid-flight, then cancel it."""
        settings = Settings.model_validate(
            {
                "output_dir": str(project / "runs"),
                "cache_dir": str(project / "cache"),
                "cache": False,
                "concurrency": concurrency,
            }
        )

        async def go():
            task = asyncio.create_task(run_eval_async(load_config(project / "eval.yaml"), settings))
            await asyncio.wait_for(state["reached"].wait(), timeout=10)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(go())
        return settings

    def test_the_partial_run_is_on_disk_and_readable(self, tmp_path, stalling_mock):
        project = self.project(tmp_path)
        settings = self.interrupt(project, stalling_mock)

        store = RunStore(settings.output_dir)
        runs = store.list_runs()
        assert len(runs) == 1, "an interrupted run left no directory behind"
        run_id = runs[0]["id"]
        assert store.load_meta(run_id)["status"] != "complete"

        records = store.load_results(run_id)
        assert 0 < len(records) < 20, f"{len(records)} of 20 cells recorded"
        # Whatever landed has to be intact: a half-written final line is
        # tolerated, a corrupt record is not.
        assert all(record.case_id for record in records)

    def test_resume_finishes_it_without_repeating_work(self, tmp_path, stalling_mock):
        project = self.project(tmp_path)
        settings = self.interrupt(project, stalling_mock)
        store = RunStore(settings.output_dir)
        run_id = store.list_runs()[0]["id"]
        done_before = {record.key for record in store.load_results(run_id)}

        stalling_mock["hang"] = False
        calls_before = stalling_mock["calls"]
        result = run_eval(load_config(project / "eval.yaml"), settings, resume_run_id=run_id)

        assert result.run_id == run_id, "resume started a new run"
        assert result.counts["total"] == 20
        keys = [record.key for record in store.load_results(run_id)]
        assert len(keys) == len(set(keys)) == 20, "resume duplicated cells"
        # The already-finished cells are skipped, not re-billed.
        assert stalling_mock["calls"] - calls_before <= 20 - len(done_before)

    def test_the_server_survives_a_cancelled_call(self, tmp_path, stalling_mock):
        """The MCP layer's half: a client giving up must not break the server."""
        project = self.project(tmp_path)

        async def go():
            from mcp.shared.memory import create_connected_server_and_client_session as connect

            server = build_server(output_dir=str(project / "runs"), config_path=None)
            async with connect(server._mcp_server) as session:
                call = asyncio.create_task(
                    session.call_tool("run_eval", {"config_path": str(project / "eval.yaml")})
                )
                await asyncio.wait_for(stalling_mock["reached"].wait(), timeout=10)
                call.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await call
                # The session is still usable, which is the point.
                listed = await session.call_tool("list_runs", {})
                return json.loads(listed.content[0].text)

        payload = asyncio.run(go())
        assert payload["total"] >= 1
        assert payload["runs"][0]["status"] != "complete"


class TestServerStartedFromAnotherDirectory:
    """Every test and hand session so far set cwd to the project.

    A real client doesn't. Claude Desktop and friends launch the server from
    whatever directory the app happens to be in, so `evaling mcp` with an
    absolute `-c` has to work — and without one it has to fail loudly rather
    than quietly evaluate something else.
    """

    CONFIG = (
        "models: [{id: mock, provider: mock}]\n"
        "variants:\n  - name: v1\n"
        '    prompt: [{role: user, content: "{{ q }}"}]\n'
        "cases: [{id: c1, vars: {q: hi}}, {id: c2, vars: {q: there}}]\n"
        "scorecard: [{criterion: acc, scorer: {type: contains, value: ''}}]\n"
    )

    @pytest.fixture
    def elsewhere(self, tmp_path):
        """A project, and an unrelated directory to launch the server from."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "eval.yaml").write_text(self.CONFIG, encoding="utf-8")
        launch = tmp_path / "launch"
        launch.mkdir()
        return project, launch

    def drive(self, cwd, args, body):
        import sys

        async def go():
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "evaling", *args, "mcp"],
                cwd=str(cwd),
                env={**os.environ, "EVALING_USER_CONFIG": str(cwd / "no-user-config.yaml")},
            )
            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session,
            ):
                await session.initialize()
                return await body(session)

        return asyncio.run(go())

    def test_an_absolute_config_works_from_anywhere(self, elsewhere):
        project, launch = elsewhere

        async def body(session):
            result = await session.call_tool("run_eval", {})
            return result.isError, json.loads(result.content[0].text)

        is_error, summary = self.drive(launch, ["-c", str(project / "eval.yaml")], body)
        assert is_error is False, summary
        assert summary["counts"]["total"] == 2

    def test_the_run_lands_with_the_config_not_the_launch_directory(self, elsewhere):
        """Otherwise the agent's runs and yours are in two different places."""
        project, launch = elsewhere

        async def body(session):
            await session.call_tool("run_eval", {})
            return None

        self.drive(launch, ["-c", str(project / "eval.yaml")], body)
        assert (project / ".evaling" / "runs").is_dir(), "runs did not land beside the config"
        assert not (launch / ".evaling").exists(), "runs landed in the launch directory"

    def test_runs_are_visible_to_the_cli_afterwards(self, elsewhere):
        """The agent and the human have to be looking at the same history."""
        project, launch = elsewhere

        async def body(session):
            await session.call_tool("run_eval", {})
            listed = await session.call_tool("list_runs", {})
            return json.loads(listed.content[0].text)

        over_mcp = self.drive(launch, ["-c", str(project / "eval.yaml")], body)
        assert over_mcp["total"] == 1

        previous = os.getcwd()
        os.chdir(project)
        try:
            result = CliRunner().invoke(
                main,
                ["--json", "list"],
                env={"EVALING_USER_CONFIG": "/nonexistent"},
                catch_exceptions=False,
            )
        finally:
            os.chdir(previous)
        assert result.exit_code == 0, result.output
        assert [row["id"] for row in json.loads(result.output)] == [
            over_mcp["runs"][0]["run_id"]
        ], "the CLI cannot see the run the agent just made"

    def test_without_a_config_the_failure_is_explicit(self, elsewhere):
        """A server launched in the wrong place must say so, not do something."""
        _, launch = elsewhere

        async def body(session):
            result = await session.call_tool("run_eval", {})
            return result.isError, result.content[0].text

        is_error, text = self.drive(launch, [], body)
        assert is_error is True
        assert "not found" in text and "eval.yaml" in text


class TestSamplingOverMcp:
    """The agent's fast loop: run a subset, then repeat the exact draw."""

    def project(self, tmp_path, cases=30):
        (tmp_path / "eval.yaml").write_text(
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: ["
            + ", ".join(f"{{id: c{i}, vars: {{q: '{i}'}}}}" for i in range(cases))
            + "]\n"
            "scorecard: [{criterion: acc, scorer: {type: contains, value: ''}}]\n",
            encoding="utf-8",
        )
        return tmp_path

    def call(self, project, **kwargs):
        return asyncio.run(
            run_eval_tool(
                config_path=str(project / "eval.yaml"),
                output_dir=str(project / "runs"),
                **kwargs,
            )
        )

    def test_a_sample_narrows_the_run_and_reports_its_seed(self, tmp_path):
        project = self.project(tmp_path)
        summary = self.call(project, sample=5)
        assert summary["counts"]["total"] == 5
        assert summary["selection"]["available"] == 30
        assert isinstance(summary["selection"]["seed"], int)

    def test_the_seed_repeats_the_draw(self, tmp_path):
        project = self.project(tmp_path)
        first = self.call(project, sample=5)
        again = self.call(project, sample=5, sample_seed=first["selection"]["seed"])

        store = RunStore(project / "runs")
        assert sorted(r.case_id for r in store.load_results(first["run_id"])) == sorted(
            r.case_id for r in store.load_results(again["run_id"])
        )

    def test_progress_totals_reflect_the_sample(self, tmp_path):
        """Otherwise the agent watches a bar that never fills."""
        project = self.project(tmp_path)
        seen = []
        asyncio.run(
            run_eval_tool(
                config_path=str(project / "eval.yaml"),
                output_dir=str(project / "runs"),
                sample=4,
                on_progress=lambda done, total: seen.append((done, total)),
            )
        )
        assert seen == [(1, 4), (2, 4), (3, 4), (4, 4)]

    def test_an_unsampled_run_reports_no_selection(self, tmp_path):
        project = self.project(tmp_path)
        assert "selection" not in self.call(project)
