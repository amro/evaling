"""MCP tools, tested directly (the transport is the SDK's job, not ours)."""

import asyncio

import pytest

from evaling.engine import run_eval
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
    (tmp_path / "eval.yaml").write_text(CONFIG)
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
        (tmp_path / "bad.yaml").write_text(CONFIG.replace("{{ q }}", "{{ missing }}"))
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
