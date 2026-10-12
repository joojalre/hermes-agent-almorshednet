"""Windows managed bridge startup under a Job that forbids breakaway; no transport."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from plugins.platforms.whatsapp import adapter as whatsapp
from plugins.platforms.whatsapp import bridge_ownership
from tests.hermes_cli.test_gateway_job_teardown_live import (
    _assign_to_job, _make_kill_on_close_job,
)

pytestmark = pytest.mark.platforms("windows")


def _adapter(tmp_path, monkeypatch):
    adapter = whatsapp.WhatsAppAdapter.__new__(whatsapp.WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter.config = SimpleNamespace(extra={})
    adapter._runtime_status_platform_key = "fixture:whatsapp"
    adapter._profile_home = tmp_path
    adapter._session_path = tmp_path / "session"
    adapter._bridge_script = str(tmp_path / "bridge.py")
    adapter._bridge_process = adapter._bridge_log_fh = None
    adapter._foreign_bridge_session = None
    adapter._running = False
    adapter._preflight = lambda: True
    adapter._ensure_bridge_deps = lambda _path: True
    adapter._acquire_platform_lock = lambda *_args: True
    adapter._release_platform_lock = MagicMock()
    adapter._reuse_running_bridge = AsyncMock(return_value=False)
    adapter._bridge_env = lambda: dict(os.environ)
    adapter._wait_for_bridge = AsyncMock(return_value=True)
    adapter._attach_to_bridge = MagicMock()
    adapter._wire_plugin_handlers = MagicMock()
    adapter._mark_connected = MagicMock(side_effect=lambda: setattr(adapter, "_running", True))
    adapter._set_fatal_error = MagicMock()
    monkeypatch.setattr(whatsapp, "find_node_executable", lambda _name: sys._base_executable)
    monkeypatch.setattr(whatsapp, "_wenv", lambda _name, default="": default)
    monkeypatch.setattr(bridge_ownership, "secondary_bridge_port", lambda *_args: 3001)
    monkeypatch.setattr(bridge_ownership, "check_secondary_ownership", lambda *_args: "free")
    return adapter


_CHILD = """
import asyncio, contextlib, io, json, subprocess, sys
from pathlib import Path
from pytest import MonkeyPatch
from tests.plugins.platforms.whatsapp.test_bridge_spawn_windows import _adapter
from plugins.platforms.whatsapp import adapter as whatsapp
cell = Path(sys.argv[1])
(cell / 'bridge.py').write_text('import threading; threading.Event().wait(120)\\n')
patcher = MonkeyPatch()
adapter = _adapter(cell, patcher)
original = subprocess.Popen
attempts = []
first_command = None
first_options = None
def popen(*args, **kwargs):
    global first_command, first_options
    if first_options is None:
        first_command, first_options = args[0], kwargs
    else:
        assert args[0] == first_command
        assert all(kwargs[key] is first_options[key] for key in ('env', 'stdin', 'stdout', 'stderr'))
    attempt = {'flags': kwargs['creationflags']}
    attempts.append(attempt)
    try:
        process = original(*args, **kwargs)
        attempt['pid'] = process.pid
        return process
    except OSError as exc:
        attempt['winerror'] = exc.winerror
        raise
patcher.setattr(whatsapp.subprocess, 'Popen', popen)
capture = io.StringIO()
with contextlib.redirect_stdout(capture):
    connected = asyncio.run(adapter.connect())
pidfile = cell / 'session/bridge.pid'
result = {'connected': connected, 'attempts': attempts,
          'owned_pid': adapter._bridge_process.pid if adapter._bridge_process else None,
          'pidfile_pid': int(pidfile.read_text().split()[0]) if pidfile.exists() else None,
          'attached': adapter._attach_to_bridge.call_count}
adapter._close_bridge_log()
patcher.undo()
print(json.dumps(result), flush=True)
"""


def test_managed_bridge_retries_real_breakaway_denial_and_records_owned_child(tmp_path):
    script = tmp_path / "launcher.py"
    script.write_text(textwrap.dedent(_CHILD), encoding="utf-8")
    env = {**os.environ, "HERMES_HOME": str(tmp_path / "home"),
           "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
           "PYTHONPATH": os.pathsep.join(sys.path), "PYTHONDONTWRITEBYTECODE": "1"}
    job = _make_kill_on_close_job(allow_breakaway=False)
    proc = None
    try:
        proc = subprocess.Popen([sys._base_executable, str(script), str(tmp_path)],
                                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                creationflags=0x4 | subprocess.CREATE_NO_WINDOW)
        _assign_to_job(job, proc)
        resume = ctypes.WinDLL("ntdll").NtResumeProcess
        resume.argtypes = [ctypes.c_void_p]
        assert resume(ctypes.c_void_p(int(proc._handle))) == 0
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, (out, err)
        result = json.loads(out.splitlines()[-1])
        assert result["attempts"][0]["winerror"] == 5
        assert result["connected"] is True, result
        assert len(result["attempts"]) == 2
        first, second = result["attempts"]
        assert first["flags"] ^ second["flags"] == 0x01000000
        assert second["flags"] & subprocess.CREATE_NO_WINDOW
        assert result["owned_pid"] == result["pidfile_pid"] == second["pid"]
        assert result["attached"] == 1
    finally:
        ctypes.windll.kernel32.CloseHandle(job)
        if proc is not None:
            proc.communicate(timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", [2, 193])
async def test_other_spawn_errors_are_not_retried_or_given_ownership(tmp_path, monkeypatch, error_code):
    adapter = _adapter(tmp_path, monkeypatch)
    error = OSError("controlled spawn failure")
    error.winerror = error_code
    spawn = MagicMock(side_effect=error)
    monkeypatch.setattr(whatsapp.subprocess, "Popen", spawn)
    assert await adapter.connect() is False
    spawn.assert_called_once()
    assert adapter._bridge_process is None
    assert not (adapter._session_path / "bridge.pid").exists()
    adapter._release_platform_lock.assert_called_once()
    adapter._attach_to_bridge.assert_not_called()


@pytest.mark.asyncio
async def test_foreign_secondary_bridge_is_not_spawned_or_adopted(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge_ownership, "check_secondary_ownership",
                        MagicMock(side_effect=ValueError("foreign bridge owns port")))
    spawn = MagicMock()
    monkeypatch.setattr(whatsapp.subprocess, "Popen", spawn)
    assert await adapter.connect() is False
    spawn.assert_not_called()
    adapter._attach_to_bridge.assert_not_called()
    adapter._set_fatal_error.assert_called_once()
