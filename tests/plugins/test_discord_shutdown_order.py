"""Discord housekeeping must stop before its HTTP client closes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["disconnect", "liveness"])
async def test_client_tasks_stop_before_http_close(monkeypatch, path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    monkeypatch.setattr(adapter, "_release_platform_lock", lambda: None)
    trace = []
    started = [asyncio.Event(), asyncio.Event()]
    workers = []
    unfinished_at_close = []

    async def work(index):
        started[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            trace.append(f"worker:{index}:stopped")

    async def close():
        unfinished_at_close.extend(
            index for index, task in enumerate(workers) if not task.done()
        )
        trace.append("client:close")

    client = SimpleNamespace(close=close, ws=None)
    adapter._client = client
    adapter._running = True
    adapter._voice_clients = {}
    notify = AsyncMock()
    adapter.set_fatal_error_handler(notify)
    adapter._set_fatal_error("discord_websocket_health_stale", "test", retryable=True)
    try:
        workers = [asyncio.create_task(work(i)) for i in range(2)]
        adapter._post_connect_task, adapter._missed_message_backfill_task = workers
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in started)), timeout=2.0
        )
        if path == "disconnect":
            await adapter.disconnect()
        else:
            await adapter._notify_liveness_fatal_error(client)
            notify.assert_awaited_once()
        assert unfinished_at_close == []
        for index in range(2):
            assert trace.index(f"worker:{index}:stopped") < trace.index("client:close")
        assert adapter._post_connect_task is None
        assert adapter._missed_message_backfill_task is None
    finally:
        for task in workers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


@pytest.mark.asyncio
async def test_disconnect_skips_own_task_and_is_repeatable(monkeypatch):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    monkeypatch.setattr(adapter, "_release_platform_lock", lambda: None)
    close = AsyncMock()
    adapter._client = SimpleNamespace(close=close)
    adapter._voice_clients = {}

    async def owner():
        current = asyncio.current_task()
        adapter._post_connect_task = current
        adapter._missed_message_backfill_task = current
        await adapter.disconnect()
        await adapter.disconnect()
        return current.cancelling()

    assert await asyncio.create_task(owner()) == 0
    assert adapter._post_connect_task is None
    assert adapter._missed_message_backfill_task is None
    close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["disconnect", "liveness"])
async def test_delayed_ready_cannot_recreate_jobs_after_teardown(monkeypatch, path):
    from plugins.platforms.discord.client_lifecycle import initialize_ready_client

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    monkeypatch.setattr(adapter, "_release_platform_lock", lambda: None)
    resolving = asyncio.Event()
    resolved = asyncio.Event()

    async def resolve():
        resolving.set()
        await resolved.wait()

    adapter._resolve_allowed_usernames = resolve
    adapter._run_post_connect_initialization = AsyncMock()
    adapter._ensure_missed_message_backfill_task = AsyncMock()
    client = SimpleNamespace(close=AsyncMock(), ws=None)
    adapter._client = client
    adapter._voice_clients = {}
    ready = asyncio.create_task(initialize_ready_client(adapter, client))
    try:
        await asyncio.wait_for(resolving.wait(), 2)
        if path == "disconnect":
            await adapter.disconnect()
        else:
            adapter._disconnecting = True
            await adapter._notify_liveness_fatal_error(client)
        resolved.set()
        await ready
        assert adapter._post_connect_task is None
        assert adapter._missed_message_backfill_task is None
        assert not adapter._ready_event.is_set()
        adapter._run_post_connect_initialization.assert_not_called()
        adapter._ensure_missed_message_backfill_task.assert_not_called()
    finally:
        ready.cancel()
        await asyncio.gather(ready, return_exceptions=True)
