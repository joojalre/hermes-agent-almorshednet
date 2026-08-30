"""Run a command in a bounded process group for hosted CI runners."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence


TIMEOUT_EXIT_CODE = 124
KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _terminate_group(pid: int, signal_number: int) -> None:
    """Signal a complete POSIX process group without racing a normal exit."""
    if os.name == "nt":
        # Windows has no killpg equivalent. taskkill's /T follows the child
        # tree, which gives local Windows verification the same no-orphans
        # guarantee as the Linux CI process group.
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    try:
        os.killpg(pid, signal_number)  # windows-footgun: ok - guarded above
    except ProcessLookupError:
        pass


def run_with_timebox(
    command: Sequence[str],
    *,
    timeout_seconds: int,
    grace_seconds: int,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    terminate_group: Callable[[int, int], None] = _terminate_group,
) -> int:
    """Run *command* and terminate its complete POSIX process group on timeout."""
    process = popen(list(command), start_new_session=os.name != "nt")
    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        print(
            f"::error::Fork test shard exceeded the {timeout_seconds}-second watchdog.",
            flush=True,
        )
        terminate_group(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                terminate_group(process.pid, KILL_SIGNAL)
            process.wait()
        return TIMEOUT_EXIT_CODE


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
