"""Shutdown watchdog + loop heartbeat coverage for #66892.

The drain path is asyncio-based; a frozen loop makes every asyncio timeout
structurally unable to fire. These tests pin the out-of-loop backstop
(thread watchdog) and the loop-liveness heartbeat file contract.
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import threading
import time
from unittest.mock import patch

import pytest

import gateway.shutdown_watchdog as shutdown_watchdog_module

from gateway.shutdown_watchdog import (
    DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S,
    arm_shutdown_watchdog,
    get_loop_heartbeat_path,
    get_shutdown_watchdog_dump_path,
    loop_heartbeat_forever,
    resolve_shutdown_watchdog_delay,
    write_loop_heartbeat,
)

def test_resolve_shutdown_watchdog_delay_adds_grace():
    assert resolve_shutdown_watchdog_delay(180) == 180 + DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    assert resolve_shutdown_watchdog_delay(0) == DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    assert resolve_shutdown_watchdog_delay("bad") == DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    assert resolve_shutdown_watchdog_delay(10, grace_s=5) == 15.0


def test_arm_shutdown_watchdog_fires_with_dump_and_exit(tmp_path):
    done = threading.Event()
    fired = threading.Event()
    dump = tmp_path / "logs" / "watchdog.log"
    snapshot_calls = []
    exit_codes = []

    def snapshot():
        snapshot_calls.append(1)
        return {"active_agents": 1, "draining": True}

    def fake_exit(code):
        exit_codes.append(code)
        fired.set()

    with patch("gateway.shutdown_watchdog.os._exit", side_effect=fake_exit):
        arm_shutdown_watchdog(
            0.15,
            done_event=done,
            snapshot_fn=snapshot,
            dump_path=dump,
            exit_code=9,
        )
        assert fired.wait(timeout=5.0), "watchdog did not fire"

    assert exit_codes == [9]
    assert snapshot_calls == [1]
    assert dump.is_file()
    text = dump.read_text(encoding="utf-8")
    assert "shutdown_watchdog_fired" in text
    assert "faulthandler dump" in text
    assert get_shutdown_watchdog_dump_path(tmp_path).name == "gateway-shutdown-watchdog.log"

@pytest.mark.asyncio
async def test_loop_tick_witness_skips_non_posix_without_warning(
    tmp_path, caplog, monkeypatch
):
    class _WindowsOsProxy:
        name = "nt"

        def __getattr__(self, item):
            return getattr(os, item)

    calls = []

    async def _forbid_start_unix_server(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("start_unix_server must not run on non-POSIX")

    monkeypatch.setattr(shutdown_watchdog_module, "os", _WindowsOsProxy())
    monkeypatch.setattr(
        shutdown_watchdog_module.asyncio,
        "start_unix_server",
        _forbid_start_unix_server,
        raising=False,
    )

    with caplog.at_level(logging.DEBUG, logger="gateway.shutdown_watchdog"):
        await shutdown_watchdog_module.loop_heartbeat_forever(
            interval_s=60.0,
            home=tmp_path,
            should_continue=lambda: False,
        )

    payload = json.loads(
        get_loop_heartbeat_path(tmp_path).read_text(encoding="utf-8")
    )
    assert calls == []
    assert payload["loop_tick_socket"] is False
    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and "Loop tick socket unavailable" in record.getMessage()
    ]

