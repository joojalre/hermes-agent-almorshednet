"""Windows control requests must leave the gateway loop free to run their work."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from gateway.control_socket import GatewayControlServer, _PipeControlProtocol, query_gateway_control
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
from tests.waiting import async_wait_until


@pytest.mark.platforms("windows")
def test_live_pipe_handler_can_marshal_to_loop_with_profile_scope(tmp_path: Path):
    home = tmp_path / "profile"
    home.mkdir()

    async def exercise():
        loop = asyncio.get_running_loop()
        progressed = asyncio.Event()

        async def on_loop():
            progressed.set()
            return {"home": str(get_hermes_home()), "completed": True}

        def handler():
            return asyncio.run_coroutine_threadsafe(on_loop(), loop).result(timeout=2.0)

        token = set_hermes_home_override(home)
        server = GatewayControlServer(home, verb_handlers={"marshal": handler})
        try:
            assert await server.start()
            result = await asyncio.to_thread(query_gateway_control, home, "marshal", timeout=8.0)
            assert result == {"home": str(home), "completed": True}
            assert progressed.is_set()
        finally:
            await server.stop()
            reset_hermes_home_override(token)

    asyncio.run(exercise())


@pytest.mark.platforms("windows")
def test_pipe_pending_request_dispatches_once_and_closes_on_error(tmp_path: Path):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def handler():
        calls.append("first")
        entered.set()
        assert release.wait(timeout=2.0), "gateway loop could not release handler"
        raise ValueError("test failure")

    class Transport:
        def __init__(self):
            self.events = []

        def write(self, data):
            self.events.append(("write", json.loads(data)))

        def close(self):
            self.events.append(("close", None))

    async def exercise():
        server = GatewayControlServer(tmp_path, verb_handlers={"first": handler})
        protocol = _PipeControlProtocol(server)
        transport = Transport()
        protocol.connection_made(transport)
        try:
            protocol.data_received(b'{"verb":"first","id":1}\n')
            assert await asyncio.to_thread(entered.wait, 2.0)
            protocol.data_received(b'{"verb":"first","id":2}\n')
            release.set()
            await async_wait_until(
                lambda: transport.events and transport.events[-1][0] == "close",
                timeout=5.0,
                what="pipe response closed",
            )
            assert calls == ["first"]
            assert [event[0] for event in transport.events] == ["write", "close"]
            response = transport.events[0][1]
            assert response["ok"] is False
            assert response["id"] == 1
            assert response["error"] == "ValueError: test failure"
        finally:
            release.set()

    asyncio.run(exercise())
