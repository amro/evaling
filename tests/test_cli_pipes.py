"""Closed output consumers and command stdin must not kill the interpreter."""

import json
import os
import signal
import subprocess
import sys

import pytest
import yaml
from click.testing import CliRunner

from evaling.cli import main


@pytest.mark.skipif(not hasattr(signal, "SIGPIPE"), reason="POSIX signal handling")
def test_cli_preserves_sigpipe_handler(tmp_path):
    previous = signal.getsignal(signal.SIGPIPE)
    try:
        result = CliRunner().invoke(main, ["-o", str(tmp_path / "runs"), "list"])
        assert result.exit_code == 0, result.output
        assert signal.getsignal(signal.SIGPIPE) == previous
    finally:
        signal.signal(signal.SIGPIPE, previous)


def test_closed_cli_output_exits_quietly(tmp_path):
    # Close the read end before launch: unlike `| head`, this cannot race
    # with a small output fitting in the pipe before the consumer exits.
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "evaling", "--json", "-o", str(tmp_path / "runs"), "list"],
            stdout=write_fd,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
    finally:
        os.close(write_fd)
    assert result.returncode == 1, result.stderr
    assert result.stderr == ""


def test_command_closing_stdin_does_not_kill_cli(tmp_path):
    worker = tmp_path / "close_stdin.py"
    worker.write_text("import os\nos.close(0)\nprint('OK')\n", encoding="utf-8")
    config = tmp_path / "eval.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "settings": {"output_dir": str(tmp_path / "runs"), "cache": False},
                "models": [
                    {
                        "id": "closed",
                        "provider": "command",
                        "command": f'"{sys.executable}" "{worker}"',
                        "timeout_s": 5,
                        "max_retries": 0,
                    }
                ],
                # Exceed the OS pipe capacity so writing encounters the
                # closed reader, even if the first chunk was buffered.
                "variants": [
                    {"name": "smoke", "prompt": [{"role": "user", "content": "x" * 1048576}]}
                ],
                "cases": [{"id": "smoke", "expected": "OK"}],
                "scorecard": [{"criterion": "exact", "scorer": {"type": "exact"}}],
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "evaling", "--json", "run", str(config)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    summary = json.loads(result.stdout)
    assert summary["counts"]["succeeded"] == 1, summary
    assert "Event loop is closed" not in result.stderr
