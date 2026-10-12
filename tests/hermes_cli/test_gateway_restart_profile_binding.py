"""Windows recovery respawns retain the paused gateway's profile identity."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from hermes_cli.gateway_windows import windowless_gateway_restart_spec


@pytest.mark.platforms("windows")
@pytest.mark.parametrize("named", [False, True], ids=["root", "named"])
def test_restart_spec_preserves_home_against_another_sticky_profile(tmp_path, monkeypatch, named):
    root = tmp_path / ".hermes"
    root.mkdir()
    for name in ("localgeneral", "worker"):
        profile = root / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("{}\n", encoding="utf-8")
    (root / "active_profile").write_text("localgeneral\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_constants._get_platform_default_hermes_home", lambda: root)
    monkeypatch.setenv("HERMES_HOME", str(root))
    for key in (
        "HERMES_SUPERVISED_CHILD", "HERMES_S6_SUPERVISED_CHILD",
        "INVOCATION_ID", "HERMES_GATEWAY_EXTERNAL_SUPERVISOR",
    ):
        monkeypatch.delenv(key, raising=False)

    # Import without arming a real gateway startup watchdog or starting a gateway.
    monkeypatch.setattr(sys, "argv", ["hermes", "--help"])
    from hermes_cli.main import _apply_profile_override

    target = root / "profiles" / "worker" if named else root
    selectors = ["--profile", "worker"] if named else []
    argv = [sys.executable, "-m", "hermes_cli.main", *selectors, "gateway", "run", "--replace"]
    replay, _cwd, overlay = windowless_gateway_restart_spec(argv, home=str(target))
    assert replay[1:] == argv[1:]
    for key, value in overlay.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["hermes", *selectors, "gateway", "run", "--replace"])
    _apply_profile_override()

    assert Path(os.environ["HERMES_HOME"]).resolve() == target.resolve()
