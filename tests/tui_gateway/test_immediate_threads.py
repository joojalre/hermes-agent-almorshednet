"""Gateway thread fixtures retain real subprocess IO and context-bound admission."""

import contextvars
import subprocess
import sys
import threading

from agent import memory_provider
from hermes_cli.backend_retirement import retirement
from tests.tui_gateway.immediate_threads import install_immediate_threads
from tui_gateway import server


def test_immediate_gateway_threads_leave_subprocess_readers_real(monkeypatch):
    original_thread = threading.Thread
    install_immediate_threads(monkeypatch, server)

    assert threading.Thread is original_thread
    with subprocess.Popen(
        [sys.executable, "-c", "import sys; print('stdout-fixture'); print('stderr-fixture', file=sys.stderr)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as child:
        stdout, stderr = child.communicate(timeout=10)

    assert child.returncode == 0
    assert stdout.strip() == "stdout-fixture"
    assert stderr.strip() == "stderr-fixture"
    assert threading.Thread is original_thread


def test_immediate_gateway_workers_capture_context_args_and_retirement(monkeypatch):
    original_thread = threading.Thread
    install_immediate_threads(monkeypatch, server)
    scope = contextvars.ContextVar("fixture-worker-scope", default="outside")
    seen = []
    token = scope.set("captured")
    try:
        worker = memory_provider.spawn_context_thread(
            lambda value, *, suffix: seen.append((scope.get(), value, suffix)),
            name="fixture-context-worker",
            args=("positional",),
            kwargs={"suffix": "keyword"},
        )
    finally:
        scope.reset(token)
    worker.start()
    worker.join()
    assert not worker.is_alive()
    assert seen == [("captured", "positional", "keyword")]
    assert scope.get() == "outside"

    active_before = retirement.active_count()
    session = {}
    token = scope.set("admitted")
    try:
        worker = server._start_session_work(
            lambda: seen.append((scope.get(), retirement.active_count())),
            name="fixture-admitted-worker",
            session=session,
        )
    finally:
        scope.reset(token)
    assert session["_run_thread"] is worker
    assert seen[-1] == ("admitted", active_before + 1)
    assert retirement.active_count() == active_before
    assert not worker.is_alive()
    assert scope.get() == "outside"
    assert threading.Thread is original_thread
