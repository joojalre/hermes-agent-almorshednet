"""Managed bridges retain ownership until Windows process-tree teardown succeeds."""

import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from plugins.platforms.whatsapp import adapter as whatsapp


def _owned_adapter(tmp_path):
    adapter = whatsapp.WhatsAppAdapter.__new__(whatsapp.WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter._session_path = tmp_path
    adapter._runtime_status_platform_key = "testprofile:whatsapp"
    adapter._bridge_process = MagicMock(pid=4242)
    adapter._bridge_process.poll.return_value = None
    adapter._bridge_process.wait.return_value = 0
    adapter._poll_task = None
    adapter._http_session = None
    adapter._release_platform_lock = MagicMock()
    adapter._mark_disconnected = MagicMock()
    adapter._close_bridge_log = MagicMock()
    (tmp_path / "bridge.pid").write_text("4242\n123456\n3001", encoding="utf-8")
    return adapter


@pytest.mark.asyncio
@pytest.mark.platforms("windows")
async def test_graceful_tree_failure_still_forces_and_reaps_owned_bridge(tmp_path, monkeypatch):
    adapter = _owned_adapter(tmp_path)
    proc = adapter._bridge_process

    def taskkill(argv, **kwargs):
        if "/F" in argv:
            proc.poll.return_value = 0
            return subprocess.CompletedProcess(argv, 0)
        return subprocess.CompletedProcess(argv, 1, stderr="graceful termination unavailable")

    run = MagicMock(side_effect=taskkill)
    monkeypatch.setattr(whatsapp.subprocess, "run", run)
    monkeypatch.setattr(whatsapp.asyncio, "sleep", AsyncMock())
    await adapter.disconnect()

    assert [call.args[0] for call in run.call_args_list] == [
        ["taskkill", "/PID", "4242", "/T"],
        ["taskkill", "/PID", "4242", "/T", "/F"],
    ]
    proc.wait.assert_called_once_with(timeout=1)
    assert adapter._bridge_process is None
    assert not (tmp_path / "bridge.pid").exists()
    adapter._mark_disconnected.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.platforms("windows")
@pytest.mark.parametrize("force_failure", ["command", "reap"])
async def test_unstopped_owned_bridge_keeps_pidfile_and_does_not_report_disconnect(
    tmp_path, monkeypatch, force_failure,
):
    adapter = _owned_adapter(tmp_path)
    proc = adapter._bridge_process
    pidfile = tmp_path / "bridge.pid"
    before = pidfile.read_bytes()
    if force_failure == "reap":
        proc.wait.side_effect = subprocess.TimeoutExpired("owned bridge", 1)

    def taskkill(argv, **kwargs):
        code = 0 if "/F" in argv and force_failure == "reap" else 1
        return subprocess.CompletedProcess(argv, code, stderr="termination unavailable")

    monkeypatch.setattr(whatsapp.subprocess, "run", MagicMock(side_effect=taskkill))
    monkeypatch.setattr(whatsapp.asyncio, "sleep", AsyncMock())
    with pytest.raises((OSError, subprocess.TimeoutExpired)):
        await adapter.disconnect()

    assert pidfile.read_bytes() == before
    assert adapter._bridge_process is proc
    adapter._release_platform_lock.assert_not_called()
    adapter._mark_disconnected.assert_not_called()


@pytest.mark.asyncio
async def test_borrowed_secondary_bridge_keeps_ownership_record(tmp_path, monkeypatch):
    adapter = _owned_adapter(tmp_path)
    adapter._bridge_process = None
    pidfile = tmp_path / "bridge.pid"
    before = pidfile.read_bytes()
    run = MagicMock()
    monkeypatch.setattr(whatsapp.subprocess, "run", run)

    await adapter.disconnect()
    await adapter.disconnect()

    run.assert_not_called()
    assert pidfile.read_bytes() == before
    assert adapter._bridge_process is None


@pytest.mark.asyncio
async def test_reaped_managed_bridge_is_not_signalled_again(tmp_path, monkeypatch):
    adapter = _owned_adapter(tmp_path)
    proc = adapter._bridge_process
    proc.poll.return_value = 0
    run = MagicMock()
    monkeypatch.setattr(whatsapp.subprocess, "run", run)

    await adapter.disconnect()

    run.assert_not_called()
    proc.wait.assert_not_called()
    assert adapter._bridge_process is None
    assert not (tmp_path / "bridge.pid").exists()
