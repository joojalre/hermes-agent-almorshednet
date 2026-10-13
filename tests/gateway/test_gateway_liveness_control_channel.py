"""Exact-home PID validation must work independently of the host control channel."""

from pathlib import Path

import pytest

from gateway import host_attach, status


def _runtime(home: Path) -> dict:
    return {
        "pid": 4242,
        "kind": "hermes-gateway",
        "gateway_state": "running",
        "start_time": 1000,
        "hermes_home": str(home),
    }


def _live_process(monkeypatch, command: str) -> None:
    monkeypatch.setattr(status, "_pid_exists", lambda pid: pid == 4242)
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: 1000)
    monkeypatch.setattr(status, "_read_process_cmdline", lambda pid: command)


def test_exact_home_liveness_does_not_dial_a_stalled_host_channel(tmp_path, monkeypatch):
    home = tmp_path / ".hermes" / "profiles" / "eagle"
    _live_process(monkeypatch, "python -m hermes_cli.main -p eagle gateway run")
    dials = []

    def stalled_host_channel():
        dials.append(True)
        raise TimeoutError("control channel unavailable")

    monkeypatch.setattr(host_attach, "host_gateway", stalled_host_channel)

    assert status.get_runtime_status_running_pid(_runtime(home), expected_home=home) == 4242
    assert not dials, "Exact-home process identity must not depend on a control-channel dial"


@pytest.mark.parametrize("served", [True, False])
def test_other_home_argv_still_requires_verified_multiplex_coverage(tmp_path, monkeypatch, served):
    home = tmp_path / ".hermes" / "profiles" / "eagle"
    _live_process(monkeypatch, "python -m hermes_cli.main gateway run")
    dials = []

    def host_serves(pid, requested_home):
        dials.append((pid, requested_home))
        return served

    monkeypatch.setattr(status, "_host_gateway_serves_home", host_serves)

    result = status.get_runtime_status_running_pid(_runtime(home), expected_home=home)

    assert result == (4242 if served else None)
    assert dials == [(4242, home)]


@pytest.mark.parametrize("invalid", ["birth", "home"])
def test_exact_home_fast_path_preserves_record_identity_guards(tmp_path, monkeypatch, invalid):
    home = tmp_path / ".hermes" / "profiles" / "eagle"
    _live_process(monkeypatch, "python -m hermes_cli.main -p eagle gateway run")
    record = _runtime(home)
    if invalid == "birth":
        record["start_time"] = 999
    else:
        record["hermes_home"] = str(tmp_path / "other" / "profiles" / "eagle")

    assert status.get_runtime_status_running_pid(record, expected_home=home) is None
