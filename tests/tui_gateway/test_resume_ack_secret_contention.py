"""A Desktop resume ACK must not wait for another profile's external secret source."""

import sys
import threading
import traceback
from pathlib import Path

import pytest

from hermes_cli import env_loader
from hermes_state import SessionDB
from tui_gateway import server


@pytest.mark.parametrize("profile", [None, "secondary"], ids=["launch", "secondary"])
@pytest.mark.parametrize("defer_history", [True, False], ids=["deferred", "cold"])
@pytest.mark.parametrize("provider", ["", "openai-api"], ids=["unrouted", "builtin"])
def test_desktop_resume_ack_precedes_secret_lock_and_history(tmp_path, monkeypatch, profile, defer_history, provider):
    launch, secondary = tmp_path / "launch", tmp_path / "secondary"
    for home in (launch, secondary):
        home.mkdir()
        (home / "config.yaml").write_text("model:\n  default: fixture-model\n  provider: openai\n")
    home = secondary if profile else launch
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(server, "_hermes_home", str(launch))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_profile_home", lambda p: secondary if p else None)
    monkeypatch.setattr(server, "_profile_configured_cwd", lambda _: str(tmp_path))
    monkeypatch.setattr(server, "_default_session_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(server, "build_profile_secret_scope", lambda _: {})
    # Retain the real public hydration function and its shared lock, without source/file access.
    monkeypatch.setattr(env_loader, "_hydrate_profile_secret_sources", lambda _: {})
    db = SessionDB(home / "state.db")
    db.create_session("stored", "desktop", model="fixture-model", model_config={
        "provider": provider, "reasoning_config": {}, "service_tier": "normal",
    })
    db.append_messages_batch("stored", [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"fixture {i}"}
        for i in range(823)
    ])
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr("hermes_state_registry.acquire", lambda *a, **k: db)
    for name in ("_enable_gateway_prompts", "_schedule_session_cap_enforcement"):
        monkeypatch.setattr(server, name, lambda: None)
    built, history_started, release_history = threading.Event(), threading.Event(), threading.Event()
    started, replied, source_entered = threading.Event(), threading.Event(), threading.Event()
    auto_continue, result = [], {}
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: built.set())
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a: built.set())
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a: auto_continue.append(a))
    monkeypatch.setattr(server, "_emit", lambda *a: None)
    original_read = db.get_messages_as_conversation
    original_hydrate = env_loader.hydrate_profile_secret_sources
    if not defer_history:
        release_history.set()

    def read(*args, **kwargs):
        history_started.set()
        assert release_history.wait(10)
        return original_read(*args, **kwargs)

    def hydrate(path):
        source_entered.set()
        return original_hydrate(path)

    def resume():
        started.set()
        try:
            result["response"] = server.handle_request({"id": "resume", "method": "session.resume", "params": {
                "session_id": "stored", "source": "desktop", "defer_history": defer_history,
                "omit_messages": True, **({"profile": profile} if profile else {}),
            }})
        except BaseException as exc:
            result["error"] = exc
        finally:
            replied.set()

    monkeypatch.setattr(db, "get_messages_as_conversation", read)
    monkeypatch.setattr(env_loader, "hydrate_profile_secret_sources", hydrate)
    worker = threading.Thread(target=resume, daemon=True)
    lock = env_loader._SECRET_SOURCE_CACHE_LOCK
    lock.acquire()
    try:
        worker.start()
        assert started.wait(5)
        if not replied.wait(5):
            stack = "".join(traceback.format_stack(sys._current_frames()[worker.ident]))
            pytest.fail(f"ACK pending; external-source entry={source_entered.is_set()}\n{stack}")
        assert "error" not in result, result
        response = result["response"]
        assert response is not None and "error" not in response, response
        payload = response["result"]
        if defer_history:
            assert payload["hydrating"] is True
        assert payload["messages"] == []
        assert payload["message_count"] == 823
        assert history_started.wait(5)
        record = server._sessions[payload["session_id"]]
        assert record["resume_runtime_overrides"] == {
            "model_override": {"model": "fixture-model", "provider": provider or None,
                               "base_url": None, "api_mode": None},
            **({"provider_override": provider} if provider else {}),
            "reasoning_config_override": {}, "service_tier_override": "",
        }
        if defer_history:
            assert not built.is_set()
            assert auto_continue == []
        assert not source_entered.is_set()
    finally:
        lock.release()
        release_history.set()
        worker.join(10)
        assert not worker.is_alive()
        if "response" in result and "result" in (result["response"] or {}):
            sid = result["response"]["result"]["session_id"]
            record = server._sessions[sid]
            if defer_history:
                assert record["resume_history_ready"].wait(10)
            assert built.wait(10)
            server._sessions.pop(sid, None)
        db.close()
