"""Config-dependent resumes still hydrate the owning profile before restoring its route."""

from pathlib import Path

import pytest

from hermes_cli import env_loader
from tui_gateway import server
from tui_gateway.session_resume_runtime import resume_runtime_overrides


@pytest.mark.parametrize("kind", ["alias", "custom", "composer", "room"])
def test_profile_dependent_resume_preserves_hydrated_route(tmp_path, monkeypatch, kind):
    launch, secondary = tmp_path / "launch", tmp_path / "secondary"
    for home in (launch, secondary):
        home.mkdir()
    (launch / "config.yaml").write_text("model:\n  default: launch-model\n  provider: nous\n")
    (secondary / "config.yaml").write_text(
        "model:\n  default: ${RESUME_FIXTURE_MODEL}\n  provider: nous\n"
        "providers:\n  fixture:\n    api: http://127.0.0.1:59999/v1\n"
        "  openai:\n    api: http://127.0.0.1:59998/v1\n")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hydrated = []
    values = {}

    def hydrate(home):
        hydrated.append(Path(home))
        values["RESUME_FIXTURE_MODEL"] = "profile-model"
        return dict(values)

    monkeypatch.setattr(env_loader, "_hydrate_profile_secret_sources", hydrate)
    monkeypatch.setattr(server, "build_profile_secret_scope", lambda home: dict(values))
    config = {"provider": "nous"}
    if kind in {"alias", "custom"}:
        config["provider"] = "openai" if kind == "alias" else "custom:fixture"
    elif kind == "composer":
        config.update(follow_profile_config=True, composer_override_profile={"model": "profile-model", "provider": "nous"})
    else:
        config["room_plumbing"] = True
    row = {"model": "saved-pick", "model_config": config}

    restored = resume_runtime_overrides(row, secondary)

    assert hydrated == [secondary]
    if kind == "room":
        assert restored == {}
    else:
        assert restored["model_override"]["model"] == "saved-pick"
        assert restored["provider_override"] == config["provider"]
    if kind == "composer":
        # A later profile edit supersedes the saved composer pick, as on the original path.
        (secondary / "config.yaml").write_text("model:\n  default: changed-model\n  provider: nous\n")
        assert resume_runtime_overrides(row, secondary) == {}


@pytest.mark.parametrize("foreign", [False, True], ids=["own-endpoint", "foreign-endpoint"])
def test_literal_resume_preserves_nested_route_without_hydration(monkeypatch, foreign):
    def unexpected_hydration(*args):
        pytest.fail("literal resume entered external source hydration")

    monkeypatch.setattr(env_loader, "hydrate_profile_secret_sources", unexpected_hydration)
    endpoint = "https://api.openai.com/v1" if foreign else "https://inference-api.nousresearch.com/v1"
    reasoning = {"enabled": True} if foreign else {}
    tier = "priority" if foreign else "normal"
    row = {"model": "saved-pick", "model_config": {
        "provider": "openai-api", "base_url": "https://api.openai.com/v1", "api_mode": "codex_responses",
        "gateway_runtime": {"provider": "nous", "base_url": endpoint, "api_mode": "chat_completions"},
        "reasoning_config": reasoning, "service_tier": tier,
    }}

    restored = resume_runtime_overrides(row, "unused-literal-profile")

    assert restored == {
        "model_override": {"model": "saved-pick", "provider": "nous", "base_url": None if foreign else endpoint,
                           "api_mode": None if foreign else "chat_completions"},
        "provider_override": "nous", "reasoning_config_override": reasoning,
        "service_tier_override": "priority" if foreign else "",
    }
