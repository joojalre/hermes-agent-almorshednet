"""Run a command inside a bounded, recursively supervised process tree."""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence

import psutil


TIMEOUT_EXIT_CODE = 124
KILL_SIGNAL = getattr(signal, "SIGKILL", 9)


class _ForwardedSignal(BaseException):
    """Carry a runner cancellation signal out of ``Popen.wait``."""

    def __init__(self, signal_number: int) -> None:
        self.signal_number = signal_number


def _capture_process_tree(pid: int) -> list[psutil.Process]:
    """Snapshot descendants before their parent can exit and reparent them."""
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []

    try:
        descendants = root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        descendants = []

    # Descendants are signalled before the wrapper. Keeping Process handles also
    # protects escalation from accidentally targeting a reused PID.
    return [*reversed(descendants), root]


def _signal_processes(processes: Sequence[psutil.Process], signal_number: int) -> None:
    """Signal every captured process, including workers in separate sessions."""
    for process in processes:
        try:
            if signal_number == KILL_SIGNAL:
                process.kill()
            else:
                process.send_signal(signal_number)
        except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
            pass


def _wait_processes(
    processes: Sequence[psutil.Process], timeout_seconds: float
) -> list[psutil.Process]:
    """Return captured processes that remain alive after the bounded wait."""
    if not processes:
        return []
    _, alive = psutil.wait_procs(list(processes), timeout=max(0.0, timeout_seconds))
    return alive


def _stop_process_tree(
    process: subprocess.Popen[bytes],
    *,
    initial_signal: int,
    grace_seconds: float,
    capture_tree: Callable[[int], list[psutil.Process]],
    signal_processes: Callable[[Sequence[psutil.Process], int], None],
    wait_processes: Callable[[Sequence[psutil.Process], float], list[psutil.Process]],
) -> None:
    """Stop a captured tree and force-kill survivors after the grace period."""
    tracked = capture_tree(process.pid)
    signal_processes(tracked, initial_signal)

    deadline = time.monotonic() + max(0.0, grace_seconds)
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        pass

    # Do not trust the wrapper's exit as proof that its separately-sessioned
    # pytest workers exited. The captured handles remain valid after reparenting.
    alive = wait_processes(tracked, max(0.0, deadline - time.monotonic()))
    if alive:
        signal_processes(alive, KILL_SIGNAL)
        wait_processes(alive, 10.0)

    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            pass
        process.wait()


def run_with_timebox(
    command: Sequence[str],
    *,
    timeout_seconds: int,
    grace_seconds: int,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    capture_tree: Callable[[int], list[psutil.Process]] = _capture_process_tree,
    signal_processes: Callable[
        [Sequence[psutil.Process], int], None
    ] = _signal_processes,
    wait_processes: Callable[
        [Sequence[psutil.Process], float], list[psutil.Process]
    ] = _wait_processes,
) -> int:
    """Run *command* and terminate every descendant on timeout or cancellation."""
    process = popen(list(command), start_new_session=True)
    previous_handlers: dict[int, signal.Handlers] = {}

    def forward_signal(signal_number: int, _frame: object) -> None:
        raise _ForwardedSignal(signal_number)

    if threading.current_thread() is threading.main_thread():
        for signal_number in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, forward_signal)

    try:
        try:
            return process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            print(
                f"::error::Fork test shard exceeded the {timeout_seconds}-second watchdog.",
                flush=True,
            )
            _stop_process_tree(
                process,
                initial_signal=signal.SIGTERM,
                grace_seconds=grace_seconds,
                capture_tree=capture_tree,
                signal_processes=signal_processes,
                wait_processes=wait_processes,
            )
            return TIMEOUT_EXIT_CODE
        except _ForwardedSignal as forwarded:
            _stop_process_tree(
                process,
                initial_signal=forwarded.signal_number,
                grace_seconds=min(grace_seconds, 10),
                capture_tree=capture_tree,
                signal_processes=signal_processes,
                wait_processes=wait_processes,
            )
            return 128 + forwarded.signal_number
    finally:
        for signal_number, previous_handler in previous_handlers.items():
            signal.signal(signal_number, previous_handler)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=int, required=True)
    parser.add_argument("--grace-seconds", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    if args.timeout_seconds <= 0 or args.grace_seconds < 0:
        parser.error("timeout must be positive, with a non-negative grace period")

    return run_with_timebox(
        command,
        timeout_seconds=args.timeout_seconds,
        grace_seconds=args.grace_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
