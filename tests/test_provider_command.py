import asyncio
import contextlib
import json
import os
import signal
import sys

import pytest

from evaling import run_eval
from evaling.config import Case, Message, ModelSpec, load_config
from evaling.providers.base import CompletionRequest, ProviderError
from evaling.providers.command import CommandProvider
from evaling.render import render_messages
from helpers import make_settings


def script(tmp_path, body, name="model.py"):
    """A python script invoked as the command, so tests are platform-portable."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return f"{sys.executable} {path}"


def run_command(command, messages=None, tmp_path=None, **spec_extra):
    spec = ModelSpec.model_validate(
        {"id": "scripted", "provider": "command", "command": command, **spec_extra}
    )
    rendered = render_messages(
        messages or [Message(role="user", content="ping")], Case(), tmp_path or "."
    )
    provider = CommandProvider(spec)
    return asyncio.run(provider.complete(CompletionRequest(model=spec, messages=rendered)))


ECHO_LAST_USER = """
import json, sys
payload = json.load(sys.stdin)
last = payload["messages"][-1]
print("".join(p.get("text", "") for p in last["parts"]), end="")
"""


def test_stdout_becomes_the_response(tmp_path):
    completion = run_command(script(tmp_path, ECHO_LAST_USER), tmp_path=tmp_path)
    assert completion.text == "ping"
    assert completion.input_tokens is None
    assert completion.cost_usd is None


def test_request_payload_shape(tmp_path):
    body = """
import json, sys
payload = json.load(sys.stdin)
print(json.dumps({"model": payload["model"], "roles": [m["role"] for m in payload["messages"]]}))
"""
    messages = [
        Message(role="system", content="be brief"),
        Message(role="user", content="hi"),
    ]
    completion = run_command(script(tmp_path, body), messages=messages, tmp_path=tmp_path)
    data = json.loads(completion.text)
    assert data["model"] == "scripted"
    assert data["roles"] == ["system", "user"]


def test_json_stdout_reports_usage(tmp_path):
    body = """
import sys, json
sys.stdin.read()
print(json.dumps({"text": "hello", "input_tokens": 7, "output_tokens": 3, "cost_usd": 0.25}))
"""
    completion = run_command(script(tmp_path, body), tmp_path=tmp_path)
    assert completion.text == "hello"
    assert completion.input_tokens == 7
    assert completion.output_tokens == 3
    assert completion.cost_usd == 0.25


def test_json_looking_text_without_text_key_stays_verbatim(tmp_path):
    body = """
import sys
sys.stdin.read()
print('{"answer": 42}', end="")
"""
    # a model that legitimately outputs JSON must not be mistaken for a usage envelope
    assert run_command(script(tmp_path, body), tmp_path=tmp_path).text == '{"answer": 42}'


def test_string_usage_numbers_are_coerced(tmp_path):
    # JSON from a shell script often quotes its numbers; that must not poison
    # the run's totals downstream.
    body = """
import sys, json
sys.stdin.read()
print(json.dumps({"text": "hi", "input_tokens": "12", "output_tokens": 4.0}))
"""
    completion = run_command(script(tmp_path, body), tmp_path=tmp_path)
    assert completion.input_tokens == 12
    assert completion.output_tokens == 4


def test_junk_usage_is_a_provider_error_not_a_crash(tmp_path):
    body = """
import sys, json
sys.stdin.read()
print(json.dumps({"text": "hi", "input_tokens": "twelve"}))
"""
    with pytest.raises(ProviderError, match="input_tokens='twelve'"):
        run_command(script(tmp_path, body), tmp_path=tmp_path)


def test_bad_usage_fails_the_cell_not_the_run(tmp_path):
    # Regression: a string token count survived into _RunTally.add, and the
    # raw TypeError there took down the whole run instead of one cell.
    (tmp_path / "model.py").write_text(
        "import sys, json\n"
        "payload = json.load(sys.stdin)\n"
        "q = payload['messages'][-1]['parts'][0]['text']\n"
        "if q == 'bad':\n"
        "    print(json.dumps({'text': 'x', 'input_tokens': ['junk']}))\n"
        "else:\n"
        "    print(json.dumps({'text': 'x', 'input_tokens': '7'}))\n",
        encoding="utf-8",
    )
    (tmp_path / "eval.yaml").write_text(
        f"models: [{{id: m, provider: command, command: '{sys.executable} model.py'}}]\n"
        "variants:\n  - name: v1\n"
        '    prompt: [{role: user, content: "{{ q }}"}]\n'
        "cases: [{id: good, vars: {q: fine}}, {id: bad, vars: {q: bad}}]\n"
        "scorecard: [{criterion: ok, scorer: {type: contains, value: x}}]\n",
        encoding="utf-8",
    )
    result = run_eval(load_config(tmp_path / "eval.yaml"), make_settings(tmp_path))
    assert result.counts == {"total": 2, "succeeded": 1, "failed": 1, "cached": 0}
    assert result.totals["input_tokens"] == 7  # the quoted count, coerced
    by_id = {record.case_id: record for record in result.records}
    assert "input_tokens" in by_id["bad"].error
    assert by_id["good"].error is None


def test_cancellation_kills_the_child_process(tmp_path, monkeypatch):
    # Regression: cancelling complete() mid-communicate left the child
    # running unreaped after the run was torn down.
    body = """
import sys, time
sys.stdin.read()
time.sleep(30)
"""
    command = script(tmp_path, body)
    spec = ModelSpec.model_validate({"id": "scripted", "provider": "command", "command": command})
    rendered = render_messages([Message(role="user", content="ping")], Case(), tmp_path)
    provider = CommandProvider(spec)

    spawned = []
    real = asyncio.create_subprocess_shell

    async def capture(*args, **kwargs):
        process = await real(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_shell", capture)

    async def scenario():
        task = asyncio.create_task(
            provider.complete(CompletionRequest(model=spec, messages=rendered))
        )
        while not spawned:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # let communicate() get in flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return spawned[0]

    process = asyncio.run(scenario())
    try:
        assert process.returncode is not None  # killed and reaped, not orphaned
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(process.pid, signal.SIGKILL)


def test_nonzero_exit_is_a_retryable_error(tmp_path):
    body = """
import sys
sys.stdin.read()
sys.stderr.write("model unavailable")
sys.exit(3)
"""
    with pytest.raises(ProviderError, match="exited 3: model unavailable") as exc_info:
        run_command(script(tmp_path, body), tmp_path=tmp_path)
    assert exc_info.value.retryable is True


def test_timeout_kills_the_process(tmp_path):
    body = """
import sys, time
sys.stdin.read()
time.sleep(30)
"""
    with pytest.raises(ProviderError, match="timed out after 0.5") as exc_info:
        run_command(script(tmp_path, body), tmp_path=tmp_path, timeout_s=0.5)
    assert exc_info.value.retryable is True


def test_media_paths_reach_the_script(tmp_path):
    (tmp_path / "pic.png").write_bytes(b"PNG")
    body = """
import json, sys
payload = json.load(sys.stdin)
parts = payload["messages"][-1]["parts"]
print(json.dumps([p.get("type") for p in parts]), end="")
"""
    messages = [
        Message.model_validate(
            {"role": "user", "content": [{"text": "look"}, {"image": "pic.png"}]}
        )
    ]
    completion = run_command(script(tmp_path, body), messages=messages, tmp_path=tmp_path)
    assert json.loads(completion.text) == ["text", "image"]


def test_supports_every_media_kind():
    # the script decides what it can handle, so nothing is rejected up front
    for kind in ("image", "file", "audio", "video"):
        assert kind in CommandProvider.SUPPORTED_MEDIA


class TestWorkingDirectory:
    """A command resolves against its config, not against the caller's cwd.

    Every other path in a config (prompts, datasets, scorers, schemas) is
    relative to the config file. A command that instead depended on where the
    user happened to be standing made those configs silently non-portable.
    """

    def test_script_runs_in_the_config_directory(self, tmp_path, monkeypatch):
        (tmp_path / "model.py").write_text(
            "import json, sys; json.load(sys.stdin); print('ran here')\n", encoding="utf-8"
        )
        (tmp_path / "eval.yaml").write_text(
            "models: [{id: m, provider: command, command: 'python3 model.py'}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: [{id: c1, vars: {q: hi}}]\n"
            "scorecard: [{criterion: ok, scorer: {type: contains, value: 'ran here'}}]\n",
            encoding="utf-8",
        )
        # Deliberately run from somewhere else entirely.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        result = run_eval(
            load_config(tmp_path / "eval.yaml"),
            make_settings(tmp_path).model_copy(update={"output_dir": tmp_path / "runs"}),
        )
        assert result.counts["failed"] == 0, [r.error for r in result.records]
        assert result.records[0].output.strip() == "ran here"
