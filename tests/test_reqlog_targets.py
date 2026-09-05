"""Logging must validate and truncate the same regular file, without blocking."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from evaling import reqlog
from evaling.errors import EvalingError


@pytest.mark.parametrize("kind", ["directory", "symlink", "dangling-symlink", "hardlink"])
@pytest.mark.parametrize("opening", [False, True])
def test_non_regular_or_linked_target_is_refused(tmp_path, kind, opening):
    target = tmp_path / "trace"
    valuable = tmp_path / "valuable"
    valuable.write_text("", encoding="utf-8")
    if kind == "directory":
        target.mkdir()
    elif kind == "hardlink":
        os.link(valuable, target)
    else:
        try:
            target.symlink_to(valuable if kind == "symlink" else tmp_path / "absent")
        except OSError:
            pytest.skip("creating symlinks requires platform permission")
    with pytest.raises(EvalingError, match="refusing to overwrite"):
        if opening:
            reqlog.RequestLog(target)
        else:
            reqlog.check_target(target, no_look=False)
    assert valuable.read_bytes() == b""
    assert not (tmp_path / "absent").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipes")
@pytest.mark.parametrize("replace_at_open", [False, True])
def test_fifo_is_refused_without_waiting_for_a_reader(tmp_path, replace_at_open):
    target = tmp_path / "fifo"
    if replace_at_open:
        target.touch()
    else:
        os.mkfifo(target)
    code = """
import os
import sys
from pathlib import Path
from evaling.reqlog import RequestLog, check_target
from evaling.errors import EvalingError
if sys.argv[2] == 'True':
    original = os.open
    def replaced(path, flags, *args, **kwargs):
        Path(path).unlink()
        os.mkfifo(path)
        return original(path, flags, *args, **kwargs)
    os.open = replaced
for call in (lambda: check_target(sys.argv[1], no_look=False), lambda: RequestLog(sys.argv[1])):
    try:
        call()
    except EvalingError:
        pass
    else:
        raise AssertionError('FIFO accepted')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(target), str(replace_at_open)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


def test_file_replaced_before_open_is_not_truncated(tmp_path, monkeypatch):
    target = tmp_path / "trace"
    target.write_text("", encoding="utf-8")
    valuable = tmp_path / "valuable"
    valuable.write_text("keep me", encoding="utf-8")
    original = os.open

    def replaced(path, flags, *args, **kwargs):
        if Path(path) == target:
            os.replace(valuable, target)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replaced)
    with pytest.raises(EvalingError, match="refusing to overwrite"):
        reqlog.RequestLog(target)
    assert target.read_text(encoding="utf-8") == "keep me"


@pytest.mark.skipif(os.name == "nt", reason="Windows disallows replacing an open file")
def test_replacement_after_validation_is_never_truncated_or_appended(tmp_path, monkeypatch):
    target = tmp_path / "trace"
    target.write_text('{"_evaling_request_log":1}\n', encoding="utf-8")
    moved = tmp_path / "original"
    original = reqlog._validate_trace

    def replaced(handle):
        original(handle)
        target.rename(moved)
        target.write_text("keep me", encoding="utf-8")

    monkeypatch.setattr(reqlog, "_validate_trace", replaced)
    log = reqlog.RequestLog(target)
    try:
        log.record(model="m")
        assert target.read_text(encoding="utf-8") == "keep me"
        assert '"model": "m"' in moved.read_text(encoding="utf-8")
    finally:
        log.close()


def test_closed_log_does_not_reopen_its_path(tmp_path):
    target = tmp_path / "trace"
    log = reqlog.RequestLog(target)
    log.close()
    log.close()
    target.write_text("keep me", encoding="utf-8")
    log.record(model="m")
    assert target.read_text(encoding="utf-8") == "keep me"


@pytest.mark.parametrize("stage", ["validation", "truncation"])
def test_failed_initialization_closes_the_file(tmp_path, monkeypatch, stage):
    captured = []
    original = reqlog._validate_trace

    def fail(*args):
        raise OSError("synthetic file failure")

    def validate(handle):
        captured.append(handle)
        if stage == "validation":
            fail()
        original(handle)
        monkeypatch.setattr(handle, "truncate", fail)

    monkeypatch.setattr(reqlog, "_validate_trace", validate)
    with pytest.raises(EvalingError, match="synthetic file failure"):
        reqlog.RequestLog(tmp_path / "trace")
    assert len(captured) == 1
    assert captured[0].closed


@pytest.mark.parametrize("fail", [False, True])
def test_engine_closes_log_even_when_provider_construction_fails(tmp_path, monkeypatch, fail):
    from evaling import engine
    from helpers import make_config, make_settings

    opened = []
    original = engine.open_log

    def capture(*args, **kwargs):
        log = original(*args, **kwargs)
        opened.append(log)
        return log

    monkeypatch.setattr(engine, "open_log", capture)
    if fail:

        def broken(*args, **kwargs):
            raise ValueError("provider construction failed")

        monkeypatch.setattr(engine, "create_provider", broken)
        with pytest.raises(ValueError, match="provider construction"):
            engine.run_eval(
                make_config(tmp_path), make_settings(tmp_path), log_requests=tmp_path / "log"
            )
    else:
        engine.run_eval(
            make_config(tmp_path), make_settings(tmp_path), log_requests=tmp_path / "log"
        )
    assert len(opened) == 1
    assert opened[0]._handle.closed
