"""Tests for the hosted-runner process-tree watchdog."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil
import pytest

from scripts.ci.timebox_process import (
    KILL_SIGNAL,
    TIMEOUT_EXIT_CODE,
    _ForwardedSignal,
    run_with_timebox,
)


class FakeProcess:
    def __init__(self, outcomes: list[int | BaseException]) -> None:
        self.outcomes = outcomes
        self.pid = 4242
        self.returncode: int | None = None

    def wait(self, timeout: float | None = None) -> int:
        if not self.outcomes and self.returncode is not None:
            return self.returncode
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        self.returncode = int(outcome)
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def _fake_tree_callbacks(
    alive_outcomes: list[list[str]],
) -> tuple[
    list[tuple[tuple[str, ...], int]],
    dict[str, Any],
]:
    tracked = ["worker", "root"]
    signals: list[tuple[tuple[str, ...], int]] = []

    def wait_processes(_processes: list[str], _timeout: float) -> list[str]:
        return alive_outcomes.pop(0) if alive_outcomes else []

    callbacks: dict[str, Any] = {
        "capture_tree": lambda _pid: tracked,
        "signal_processes": lambda processes, signum: signals.append((
            tuple(processes),
            signum,
        )),
        "wait_processes": wait_processes,
    }
    return signals, callbacks


def test_returns_command_exit_code() -> None:
    process = FakeProcess([0])

    result = run_with_timebox(
        ["tests-command"],
        timeout_seconds=10,
        grace_seconds=2,
        popen=lambda *args, **kwargs: process,
    )

    assert result == 0


def test_timeout_terminates_the_complete_process_tree() -> None:
    process = FakeProcess([subprocess.TimeoutExpired(["tests-command"], 10), -15])
    signals, callbacks = _fake_tree_callbacks([[]])

    result = run_with_timebox(
        ["tests-command"],
        timeout_seconds=10,
        grace_seconds=2,
        popen=lambda *args, **kwargs: process,
        **callbacks,
    )

    assert result == TIMEOUT_EXIT_CODE
    assert signals == [(("worker", "root"), signal.SIGTERM)]


def test_timeout_escalates_to_kill_after_grace_period() -> None:
    process = FakeProcess([
        subprocess.TimeoutExpired(["tests-command"], 10),
        subprocess.TimeoutExpired(["tests-command"], 2),
        -9,
    ])
    signals, callbacks = _fake_tree_callbacks([["worker"], []])

    result = run_with_timebox(
        ["tests-command"],
        timeout_seconds=10,
        grace_seconds=2,
        popen=lambda *args, **kwargs: process,
        **callbacks,
    )

    assert result == TIMEOUT_EXIT_CODE
    assert signals == [
        (("worker", "root"), signal.SIGTERM),
        (("worker",), KILL_SIGNAL),
    ]


def test_timeout_escalates_when_group_leader_exits_but_descendant_survives() -> None:
    """A dead wrapper must not hide a still-running worker process."""
    process = FakeProcess([
        subprocess.TimeoutExpired(["tests-command"], 10),
        -15,
    ])
    signals, callbacks = _fake_tree_callbacks([["worker"], []])

    result = run_with_timebox(
        ["tests-command"],
        timeout_seconds=10,
        grace_seconds=2,
        popen=lambda *args, **kwargs: process,
        **callbacks,
    )

    assert result == TIMEOUT_EXIT_CODE
    assert signals == [
        (("worker", "root"), signal.SIGTERM),
        (("worker",), KILL_SIGNAL),
    ]


def test_runner_signal_is_forwarded_to_the_process_tree() -> None:
    process = FakeProcess([_ForwardedSignal(signal.SIGTERM), -15])
    signals, callbacks = _fake_tree_callbacks([[]])

    result = run_with_timebox(
        ["tests-command"],
        timeout_seconds=10,
        grace_seconds=2,
        popen=lambda *args, **kwargs: process,
        **callbacks,
    )

    assert result == 128 + signal.SIGTERM
    assert signals == [(("worker", "root"), signal.SIGTERM)]


def test_timeout_kills_descendant_in_a_separate_session(tmp_path: Path) -> None:
    """Match run_tests_parallel.py, which starts each pytest in a new session."""
    child_script = tmp_path / "child.py"
    parent_script = tmp_path / "parent.py"
    pid_file = tmp_path / "child.pid"
    child_script.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    parent_script.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, sys.argv[1]], "
        "start_new_session=True)\n"
        "Path(sys.argv[2]).write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    child_pid: int | None = None
    try:
        result = run_with_timebox(
            [sys.executable, str(parent_script), str(child_script), str(pid_file)],
            timeout_seconds=2,
            grace_seconds=1,
        )
        assert result == TIMEOUT_EXIT_CODE
        child_pid = int(pid_file.read_text(encoding="utf-8"))

        deadline = time.monotonic() + 3
        while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not psutil.pid_exists(child_pid)
    finally:
        if child_pid is not None and psutil.pid_exists(child_pid):
            child = psutil.Process(child_pid)
            child.kill()
            child.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no catchable SIGTERM")
def test_runner_sigterm_cleans_up_a_separate_session_descendant(
    tmp_path: Path,
) -> None:
    """A GitHub cancellation must be forwarded before the watchdog exits."""
    child_script = tmp_path / "cancel-child.py"
    parent_script = tmp_path / "cancel-parent.py"
    pid_file = tmp_path / "cancel-child.pid"
    child_script.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    parent_script.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, sys.argv[1]], "
        "start_new_session=True)\n"
        "Path(sys.argv[2]).write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    watchdog = subprocess.Popen([
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "scripts/ci/timebox_process.py"),
        "--timeout-seconds",
        "30",
        "--grace-seconds",
        "1",
        "--",
        sys.executable,
        str(parent_script),
        str(child_script),
        str(pid_file),
    ])
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        child_pid = int(pid_file.read_text(encoding="utf-8"))

        watchdog.send_signal(signal.SIGTERM)
        assert watchdog.wait(timeout=10) == 128 + signal.SIGTERM

        deadline = time.monotonic() + 3
        while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not psutil.pid_exists(child_pid)
    finally:
        if watchdog.poll() is None:
            watchdog.kill()
            watchdog.wait(timeout=5)
        if child_pid is not None and psutil.pid_exists(child_pid):
            child = psutil.Process(child_pid)
            child.kill()
            child.wait(timeout=5)
