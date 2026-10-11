"""Bounded teardown of jobs that share the Discord HTTP client."""
import asyncio
import logging

from agent.async_utils import consume_detached_task_result

logger = logging.getLogger(__name__)


async def initialize_ready_client(adapter, client) -> None:
    """A delayed READY from a retired client must not recreate HTTP jobs."""
    if adapter._disconnecting or adapter._client is not client:
        return
    await adapter._resolve_allowed_usernames()
    if adapter._disconnecting or adapter._client is not client:
        return
    adapter._ready_event.set()
    if adapter._post_connect_task and not adapter._post_connect_task.done():
        adapter._post_connect_task.cancel()
    adapter._post_connect_task = asyncio.create_task(adapter._run_post_connect_initialization())
    if adapter._missed_message_backfill_enabled():
        adapter._ensure_missed_message_backfill_task()


async def cancel_client_jobs(adapter) -> None:
    """Cancel both producers before waiting; never cancel or await the caller."""
    current = asyncio.current_task()
    tasks = set()
    for name in ("_post_connect_task", "_missed_message_backfill_task"):
        task = getattr(adapter, name, None)
        setattr(adapter, name, None)
        if task is not None and task is not current:
            tasks.add(task)
    if not tasks:
        return
    for task in tasks:
        if not task.done():
            task.cancel()
    done, pending = await asyncio.wait(tasks, timeout=1.0)
    for task in done:
        consume_detached_task_result(task)
    for task in pending:
        task.add_done_callback(consume_detached_task_result)
    if pending:
        logger.warning("[%s] %d Discord client job(s) exceeded the shutdown deadline",
                       adapter.name, len(pending))
