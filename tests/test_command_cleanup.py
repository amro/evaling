"""Real process trees and pipes must be gone before a command call returns."""

import asyncio
import contextlib
import ctypes
import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from evaling.config import ModelSpec
from evaling.providers import command as command_module
from evaling.providers.base import ProviderError
from evaling.providers.command import CommandProvider


def running(pid):
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    # A zombie has exited; only its parent can reap it. It holds no pipes.
    return bool(state) and not state.startswith("Z")


async def ready(paths):
    deadline = asyncio.get_running_loop().time() + 10
    while not all(path.exists() and path.read_text(encoding="utf-8").strip() for path in paths):
        assert asyncio.get_running_loop().time() < deadline, "command tree never became ready"
        await asyncio.sleep(0.01)
    return [int(path.read_text(encoding="utf-8")) for path in paths]


@pytest.mark.parametrize("stop", ["timeout", "cancel", "spawn-cancel"])
@pytest.mark.parametrize("shell_exits", [False, True])
def test_timeout_and_cancellation_stop_descendants_and_close_pipes(
    tmp_path, monkeypatch, stop, shell_exits
):
    if shell_exits and os.name == "nt":
        pytest.skip("Windows taskkill requires the command shell to remain alive")
    child = tmp_path / "child.py"
    parent = tmp_path / "parent.py"
    child.write_text(
        "import os, pathlib, threading\n"
        "pathlib.Path('child.pid').write_text(str(os.getpid()), encoding='utf-8')\n"
        "threading.Event().wait(30)\n",
        encoding="utf-8",
    )
    parent_code = (
        "import os, pathlib, subprocess, sys, threading\n"
        "sys.stdin.read()\n"
        "subprocess.Popen([sys.executable, 'child.py'], stdin=subprocess.DEVNULL)\n"
        "pathlib.Path('parent.pid').write_text(str(os.getpid()), encoding='utf-8')\n"
        "threading.Event().wait(30)\n"
    )
    if stop == "spawn-cancel":
        parent_code = parent_code.replace("sys.stdin.read()\n", "")
    parent.write_text(parent_code, encoding="utf-8")
    command = f'"{sys.executable}" "{parent}"'
    # Keep the shell distinct from its child, rather than relying on whether
    # /bin/sh happens to optimize the final command into exec().
    command += " &" if shell_exits else " && echo finished"
    spec = ModelSpec.model_validate(
        {"id": "tree", "provider": "command", "command": command, "timeout_s": 60}
    )
    provider = CommandProvider(spec, base_dir=tmp_path)
    paths = [tmp_path / "parent.pid", tmp_path / "child.pid"]
    spawned = []
    real_spawn = asyncio.create_subprocess_shell
    real_wait = asyncio.wait_for
    timeout_trigger = asyncio.Event()
    spawn_release = asyncio.Event()

    async def capture(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        spawned.append(process)
        if stop == "spawn-cancel":
            await spawn_release.wait()
        return process

    async def timeout_after_ready(awaitable, timeout):
        if timeout != 60:
            return await real_wait(awaitable, timeout)
        pending = asyncio.ensure_future(awaitable)
        await ready(paths)
        await timeout_trigger.wait()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "create_subprocess_shell", capture)
    if stop == "timeout":
        # Trigger the timeout only after both descendants demonstrably exist.
        # This cancels its awaitable exactly as wait_for does, without a race
        # between interpreter startup and a wall-clock timeout on busy CI.
        monkeypatch.setattr(asyncio, "wait_for", timeout_after_ready)

    async def scenario():
        task = asyncio.create_task(provider._run("{}"))
        pids = []
        try:
            pids = await ready(paths)
            assert all(running(pid) for pid in pids), "test never launched a live tree"
            if stop in ("cancel", "spawn-cancel"):
                task.cancel()
                await asyncio.sleep(0)
                spawn_release.set()
            else:
                timeout_trigger.set()
            error = ProviderError if stop == "timeout" else asyncio.CancelledError
            with pytest.raises(error):
                await real_wait(asyncio.shield(task), 8)
            assert not any(running(pid) for pid in pids), "command left a live descendant"
            process = spawned[0]
            assert process.returncode is not None
            assert process.stdin.is_closing()
            assert process.stdout.at_eof() and process.stderr.at_eof()
            assert process._transport.is_closing()
        finally:
            spawn_release.set()
            # Failed regressions must not reproduce the orphan leak in CI.
            for path in paths:
                if path.exists() and path.read_text(encoding="utf-8").strip():
                    pid = int(path.read_text(encoding="utf-8"))
                    if running(pid):
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(pid, signal.SIGTERM)
            for process in spawned:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                process._transport.close()
                await process.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_repeated_cancellation_cannot_abandon_cleanup(monkeypatch):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        finished = []

        async def stop(*args):
            started.set()
            await release.wait()
            finished.append(True)

        monkeypatch.setattr(command_module, "_stop_command", stop)
        task = asyncio.create_task(command_module._cleanup_command(None, None))
        await started.wait()
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "cancellation abandoned the cleanup task"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == [True]

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "pipe-error"])
def test_cleanup_closes_transport_when_pipe_drain_fails(monkeypatch, failure):
    async def scenario():
        events = []

        async def wait():
            events.append("reaped")

        process = SimpleNamespace(
            pid=123,
            returncode=0,
            wait=wait,
            _transport=SimpleNamespace(close=lambda: events.append("closed")),
        )
        if os.name == "posix":

            def gone(*args):
                raise ProcessLookupError

            monkeypatch.setattr(os, "killpg", gone)

        async def communicate():
            if failure == "timeout":
                await asyncio.Event().wait()
            raise OSError("synthetic pipe failure")

        communication = asyncio.create_task(communicate())
        if failure == "timeout":
            monkeypatch.setattr(command_module, "_CLEANUP_TIMEOUT_S", 0.01)
        await command_module._stop_command(process, communication)
        assert events == ["closed", "reaped"]
        assert communication.done()
        if failure == "timeout":
            assert communication.cancelled()

    asyncio.run(scenario())


def test_cancellation_during_failed_spawn_preserves_cancellation(monkeypatch):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def fail_spawn(*args, **kwargs):
            started.set()
            await release.wait()
            raise OSError("synthetic spawn failure")

        monkeypatch.setattr(asyncio, "create_subprocess_shell", fail_spawn)
        spec = ModelSpec.model_validate({"id": "tree", "provider": "command", "command": "unused"})
        task = asyncio.create_task(CommandProvider(spec)._run("{}"))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_windows_tree_kill_is_scoped_and_bounded(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    command_module._kill_windows_tree(123)
    [((argv,), options)] = calls
    assert argv[1:] == ["/PID", "123", "/T", "/F"]
    assert argv[0].endswith("taskkill.exe")
    assert options["timeout"] == command_module._CLEANUP_TIMEOUT_S
    assert all(options[stream] == subprocess.DEVNULL for stream in ("stdin", "stdout", "stderr"))
    assert not options.get("shell", False)


@pytest.mark.parametrize(
    "failure", [OSError("unavailable"), subprocess.TimeoutExpired("taskkill", 5)]
)
def test_windows_tree_kill_failure_still_reaps_shell_and_closes_pipes(monkeypatch, failure):
    async def scenario():
        events = []

        def kill_tree(pid):
            assert pid == 123
            raise failure

        def kill_shell():
            process.returncode = -1
            events.append("killed")

        async def wait():
            events.append("reaped")

        async def communicate():
            return b"", b""

        process = SimpleNamespace(
            pid=123,
            returncode=None,
            kill=kill_shell,
            wait=wait,
            _transport=SimpleNamespace(close=lambda: events.append("closed")),
        )
        monkeypatch.setattr(command_module, "os", SimpleNamespace(name="nt"))
        monkeypatch.setattr(command_module, "_kill_windows_tree", kill_tree)
        await command_module._stop_command(process, asyncio.create_task(communicate()))
        assert events == ["killed", "closed", "reaped"]

    asyncio.run(scenario())
