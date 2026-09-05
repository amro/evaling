"""Source failures must not bypass no-look redaction on any public surface."""

import asyncio
import json
import traceback

import pytest
from click.testing import CliRunner

from evaling.cli import main
from evaling.config import load_config
from evaling.engine import dry_run, run_eval
from evaling.mcp_server import build_server
from evaling.sources import SourceError, source_errors

CANARY = "customer-private-Z9@example.test"


def source_project(tmp_path, stage, *, no_look=True, exception="RuntimeError"):
    code = f"""
from pathlib import Path
from evaling import BaseCaseSource, Case, CasePage

class PrivateTimeout(BaseException):
    pass

CANARY = {CANARY!r}
STAGE = {stage!r}
if STAGE == "import":
    raise RuntimeError(CANARY)

class Source(BaseCaseSource):
    def __init__(self):
        if STAGE == "factory":
            raise RuntimeError(CANARY)

    def count(self):
        Path(__file__).with_name("counted").touch()
        if STAGE == "count":
            raise RuntimeError(CANARY)
        return None

    def fetch(self, cursor, limit):
        if STAGE == "fetch":
            raise RuntimeError(CANARY)
        if STAGE == "cursor":
            return CasePage(cases=[], cursor=CANARY)
        if STAGE == "attachment":
            return CasePage(cases=[Case(id="c", files={{"note": "../" + CANARY}})])
        return CasePage(cases=[Case(id="c")])

    def close(self):
        Path(__file__).with_name("closed").touch()
        if STAGE == "close":
            raise RuntimeError(CANARY)
"""
    code = code.replace("raise RuntimeError(CANARY)", f"raise {exception}(CANARY)")
    (tmp_path / "source.py").write_text(code, encoding="utf-8")
    config = {
        "settings": {"output_dir": str(tmp_path / "runs"), "cache": False},
        "models": [{"id": "mock", "provider": "mock"}],
        "variants": [{"name": "v", "prompt": [{"role": "user", "content": "hello"}]}],
        "cases": {"source": "source.py:Source", "limit": 3},
        "scorecard": [{"criterion": "ok", "scorer": {"type": "contains", "value": "hello"}}],
        "privacy": {"no_look": no_look},
    }
    path = tmp_path / "eval.yaml"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "stage", ["import", "factory", "count", "fetch", "cursor", "attachment", "close"]
)
@pytest.mark.parametrize("surface", ["engine-run", "engine-dry", "cli-run", "cli-dry", "mcp"])
@pytest.mark.parametrize("exception", ["RuntimeError", "PrivateTimeout", "SystemExit"])
def test_source_error_is_private(tmp_path, stage, surface, exception):
    if stage == "count" and surface not in ("engine-dry", "cli-dry"):
        # Do not silently skip this assumption: if runs start counting, this
        # must fail and the privacy matrix must grow to cover those calls.
        path = source_project(tmp_path, stage, exception=exception)
        if surface == "engine-run":
            run_eval(load_config(path))
        elif surface == "cli-run":
            result = CliRunner().invoke(main, ["run", str(path)])
            assert result.exit_code == 0, result.output
        else:
            asyncio.run(build_server().call_tool("run_eval", {"config_path": str(path)}))
        assert not (tmp_path / "counted").exists()
        assert (tmp_path / "closed").exists()
        return
    path = source_project(tmp_path, stage, exception=exception)
    if surface.startswith("cli"):
        args = ["run", str(path)]
        if surface == "cli-dry":
            args.append("--dry-run")
        result = CliRunner().invoke(main, args)
        assert result.exit_code == 2, result.output
        text = result.output
    else:
        with pytest.raises(Exception) as caught:
            if surface == "mcp":
                asyncio.run(build_server().call_tool("run_eval", {"config_path": str(path)}))
            elif surface == "engine-dry":
                dry_run(load_config(path))
            else:
                run_eval(load_config(path))
        text = "".join(traceback.format_exception(caught.value))
    assert "detail withheld (no-look)" in text
    assert CANARY not in text
    if stage not in ("import", "factory"):
        assert (tmp_path / "closed").exists(), "redaction must not bypass source cleanup"
    for artifact in (tmp_path / "runs").rglob("*"):
        if artifact.is_file():
            assert CANARY.encode() not in artifact.read_bytes()


@pytest.mark.parametrize(
    "stage", ["import", "factory", "count", "fetch", "cursor", "attachment", "close"]
)
def test_normal_source_errors_remain_actionable(tmp_path, stage):
    path = source_project(tmp_path, stage, no_look=False)
    with pytest.raises(Exception, match=CANARY):
        dry_run(load_config(path))


def test_source_exit_message_is_withheld():
    with (
        pytest.raises(SourceError, match="detail withheld") as caught,
        source_errors(no_look=True),
    ):
        raise SystemExit(CANARY)
    assert CANARY not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("kind", [KeyboardInterrupt, asyncio.CancelledError, GeneratorExit])
@pytest.mark.parametrize("no_look", [True, False])
def test_source_cancellation_is_not_converted_to_an_error(kind, no_look):
    original = kind()
    with pytest.raises(kind) as caught, source_errors(no_look=no_look):
        raise original
    assert caught.value is original


def test_normal_base_exception_is_unchanged():
    original = BaseException(CANARY)
    with pytest.raises(BaseException) as caught, source_errors(no_look=False):
        raise original
    assert caught.value is original
