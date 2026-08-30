"""Tests for the hosted-runner process-group watchdog."""

from __future__ import annotations

import signal
import subprocess

from scripts.ci.timebox_process import KILL_SIGNAL, TIMEOUT_EXIT_CODE, run_with_timebox


class FakeProcess:
    def __init__(self, outcomes: list[int | BaseException]) -> None:
        self.outcomes = outcomes
        self.pid = 4242

    def wait(self, timeout: int | None = None) -> int:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return int(outcome)

    def poll(self) -> None:
        return None


def test_returns_command_exit_code() -> None:
    process = FakeProcess([0])

    result = run_with_timebox(
        ["tests-command"],
        timeout_seconds=10,
        grace_seconds=2,
        popen=lambda *args, **kwargs: process,
    )

    assert result == 0


def test_timeout_terminates_the_process_group() -> None:
    process = FakeProcess([subprocess.TimeoutExpired(["tests-command"], 10), -15])
    signals: list[tuple[int, int]] = []

    result = run_with_timebox(
        ["tests-command"],
        timeout_seconds=10,
        grace_seconds=2,
        popen=lambda *args, **kwargs: process,
        terminate_group=lambda pid, signum: signals.append((pid, signum)),
    )

    assert result == TIMEOUT_EXIT_CODE
    assert signals == [(4242, signal.SIGTERM)]


def test_timeout_escalates_to_kill_after_grace_period() -> None:
    process = FakeProcess(
        [
            subprocess.TimeoutExpired(["tests-command"], 10),
            subprocess.TimeoutExpired(["tests-command"], 2),
            -9,
        ]
    )
    signals: list[tuple[int, int]] = []

    result = run_with_timebox(
        ["tests-command"],
        timeout_seconds=10,
        grace_seconds=2,
        popen=lambda *args, **kwargs: process,
        terminate_group=lambda pid, signum: signals.append((pid, signum)),
    )

    assert result == TIMEOUT_EXIT_CODE
    assert signals == [(4242, signal.SIGTERM), (4242, KILL_SIGNAL)]
