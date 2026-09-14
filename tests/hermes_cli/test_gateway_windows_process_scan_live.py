"""Native Windows coverage for the gateway process-table fallback.

The fast path must inspect real Windows process command lines without starting
PowerShell/WMIC.  The sleepers below only carry Hermes-shaped argv; they never
import or execute the gateway, and every owned child is reaped on scope exit.
"""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from hermes_cli import gateway, gateway_windows


pytestmark = pytest.mark.windows_only


def _spawn_gateway_shaped_sleeper(profile: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            getattr(sys, "_base_executable", sys.executable),
            "-c",
            "import time; time.sleep(120)",
            "-m",
            "hermes_cli.main",
            "--profile",
            profile,
            "gateway",
            "run",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def _stop_owned(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.kill()
    child.wait(timeout=10)


@pytest.mark.spawns_gateway_lookalike
def test_native_scan_avoids_wmi_and_preserves_profile_scope_and_exclusions(
    tmp_path, monkeypatch
):
    profile = f"scan-{os.getpid()}"
    sibling_profile = f"{profile}-2"
    profile_home = tmp_path / "hermes-home" / "profiles" / profile
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    with ExitStack() as owned_children:
        own = _spawn_gateway_shaped_sleeper(profile)
        owned_children.callback(_stop_owned, own)
        sibling = _spawn_gateway_shaped_sleeper(sibling_profile)
        owned_children.callback(_stop_owned, sibling)

        def _wmi_must_not_run():
            raise AssertionError("native psutil snapshot unexpectedly fell back to WMIC/PowerShell")

        monkeypatch.setattr(gateway, "_windows_process_listing", _wmi_must_not_run)

        scoped = gateway._scan_gateway_pids(set())
        assert own.pid in scoped
        assert sibling.pid not in scoped

        excluded = gateway._scan_gateway_pids({own.pid})
        assert own.pid not in excluded
        assert sibling.pid not in excluded

        all_profiles = gateway._scan_gateway_pids(set(), all_profiles=True)
        assert own.pid in all_profiles
        assert sibling.pid in all_profiles


def test_scan_retains_bounded_listing_fallback_when_native_snapshot_is_unavailable(
    tmp_path, monkeypatch
):
    profile = f"fallback-{os.getpid()}"
    profile_home = tmp_path / "hermes-home" / "profiles" / profile
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setattr(gateway_windows, "_snapshot_process_command_lines", lambda: None)

    calls = []

    def _fallback_listing():
        calls.append(True)
        return "\n".join(
            [
                f"CommandLine=python.exe -m hermes_cli.main --profile {profile} gateway run",
                "ProcessId=11001",
                f"CommandLine=python.exe -m hermes_cli.main --profile {profile}-2 gateway run",
                "ProcessId=11002",
                f"CommandLine=python.exe -m hermes_cli.main --profile {profile} gateway run",
                "ProcessId=11003",
            ]
        )

    monkeypatch.setattr(gateway, "_windows_process_listing", _fallback_listing)

    assert gateway._scan_gateway_pids({11003}) == [11001]
    assert calls == [True]


def test_native_snapshot_preserves_argument_boundaries_and_fails_over_cleanly(monkeypatch):
    psutil = pytest.importorskip("psutil")
    argv = [
        r"C:\Program Files\Hermes\python.exe",
        "-m",
        "hermes_cli.main",
        "--profile",
        "work",
        "gateway",
        "run",
    ]
    entries = [
        SimpleNamespace(info={"pid": 12001, "cmdline": argv}),
        SimpleNamespace(info={"pid": 12002, "cmdline": None}),
        SimpleNamespace(info={"pid": 12003, "cmdline": []}),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda *args, **kwargs: iter(entries))

    assert gateway_windows._snapshot_process_command_lines() == [
        (12001, subprocess.list2cmdline(argv))
    ]

    def _failed_snapshot(*_args, **_kwargs):
        raise psutil.Error("synthetic process-table failure")

    monkeypatch.setattr(psutil, "process_iter", _failed_snapshot)
    assert gateway_windows._snapshot_process_command_lines() is None
