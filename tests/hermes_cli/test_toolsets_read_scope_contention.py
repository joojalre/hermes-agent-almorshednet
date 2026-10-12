"""Toolset reads retain profile isolation without joining skills-directory mutations."""

import asyncio
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_toolsets_read_completes_while_skills_scope_is_locked(tmp_path, monkeypatch):
    from agent import secret_scope
    from hermes_cli import plugins, tools_config, web_server_profiles
    from hermes_cli.config import atomic_config_write
    from hermes_cli.web_routers import tools
    from hermes_constants import get_hermes_home
    from tools import skill_manager_tool, skills_tool  # warm the real skills-scope imports

    default_home = tmp_path / ".hermes"
    full_home = default_home / "profiles" / "full"
    full_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    # These are synthetic test markers, never real provider credentials.
    (default_home / ".env").write_text(
        "TOOLSETS_SCOPE_TEST_KEY=default-marker\nTOOLSETS_DEFAULT_ONLY=default-only\n", encoding="utf-8")
    (full_home / ".env").write_text("TOOLSETS_SCOPE_TEST_KEY=full-marker\n", encoding="utf-8")
    for home in (default_home, full_home):
        atomic_config_write(home / "config.yaml", {"terminal": {"cwd": str(home)}})

    expected = {default_home: "default-marker", full_home: "full-marker"}
    observed = set()

    def check_scope(config):
        home = get_hermes_home()
        assert home in expected
        assert config["terminal"]["cwd"] == str(home)
        assert secret_scope.current_secret_scope_home() == str(home)
        assert secret_scope.get_secret("TOOLSETS_SCOPE_TEST_KEY") == expected[home]
        assert bool(secret_scope.get_secret("TOOLSETS_DEFAULT_ONLY")) == (home == default_home)
        observed.add(home)

    def account_features(config):
        check_scope(config)
        return object()

    def provider_ready(name, config, *, features):
        check_scope(config)
        return get_hermes_home() == full_home

    monkeypatch.setattr(plugins, "discover_plugins", lambda: None)
    monkeypatch.setattr(plugins, "get_plugin_toolsets", list)
    monkeypatch.setattr(tools_config, "get_nous_subscription_features", account_features)
    monkeypatch.setattr(tools_config, "_toolset_has_keys", provider_ready)
    # Keep the actual module globals in place: only config/secret scoping may be used by this GET.
    skill_globals = [(m.HERMES_HOME, m.SKILLS_DIR) for m in (skills_tool, skill_manager_tool)]
    app = FastAPI()
    app.include_router(tools.router)
    locked = threading.Event()
    release = threading.Event()

    def hold_skills_scope():
        with web_server_profiles._SKILLS_PROFILE_LOCK:
            locked.set()
            release.wait(timeout=30)

    holder = threading.Thread(target=hold_skills_scope, name="test-skills-scope-holder")
    holder.start()
    requests = []
    try:
        assert await asyncio.to_thread(locked.wait, 5)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            requests = [asyncio.create_task(client.get("/api/tools/toolsets", params={"profile": profile}))
                        for profile in ("default", "full")]
            try:
                done, pending = await asyncio.wait(requests, timeout=3)
                assert not pending, "Toolset config reads waited for the unrelated skills-directory lock"
                assert len(done) == 2
                for request, profile in zip(requests, ("default", "full")):
                    response = request.result()
                    assert response.status_code == 200
                    rows = response.json()
                    assert rows
                    assert all(row["configured"] == (profile == "full") for row in rows)
                assert observed == {default_home, full_home}
                assert [(m.HERMES_HOME, m.SKILLS_DIR) for m in (skills_tool, skill_manager_tool)] == skill_globals
            finally:
                release.set()
                await asyncio.gather(*requests, return_exceptions=True)
    finally:
        release.set()
        await asyncio.to_thread(holder.join, 5)
        assert not holder.is_alive()
