"""Device-code login URLs belong to the bound profile, including billing step-up.

The real login function runs through fake HTTP, device-code and token seams;
these tests neither contact a provider nor read or persist an account.
"""

from contextlib import nullcontext
from pathlib import Path

import pytest

from agent import secret_scope
from hermes_cli import auth, auth_nous


@pytest.fixture
def device_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    monkeypatch.setenv("HERMES_PORTAL_BASE_URL", "https://launch-portal.invalid/")
    monkeypatch.setenv("NOUS_PORTAL_BASE_URL", "https://launch-alias.invalid/")
    monkeypatch.setenv("NOUS_INFERENCE_BASE_URL", "https://launch-inference.invalid/")
    secret_token = secret_scope.set_secret_scope(None)
    secret_scope.set_multiplex_active(False)
    observed = []

    def forbidden(*args, **kwargs):
        pytest.fail("device URL test must not read an account or perform network I/O")

    def request_device_code(**kwargs):
        observed.append(("device", kwargs["portal_base_url"], kwargs["scope"]))
        return {
            "verification_uri_complete": "https://verification.invalid/synthetic",
            "user_code": "synthetic-code", "device_code": "synthetic-device",
            "expires_in": 600, "interval": 1,
        }

    def poll_for_token(**kwargs):
        observed.append(("poll", kwargs["portal_base_url"], None))
        return {"access_token": "synthetic-access", "expires_in": 3600}

    import httpx
    import urllib.request

    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(auth, "get_provider_auth_state", forbidden)
    monkeypatch.setattr(auth, "_save_active_provider_state", forbidden)
    monkeypatch.setattr(auth_nous, "_nous_http_client", lambda *args: nullcontext(object()))
    monkeypatch.setattr(auth, "_is_remote_session", lambda: False)
    monkeypatch.setattr(auth, "_request_device_code", request_device_code)
    monkeypatch.setattr(auth, "_poll_for_token", poll_for_token)
    monkeypatch.setattr(auth, "_print_device_code_instructions", lambda *args, **kwargs: None)
    monkeypatch.setattr(auth, "refresh_nous_oauth_from_state", lambda state, **kwargs: state)
    try:
        yield observed
    finally:
        secret_scope.set_multiplex_active(False)
        secret_scope.reset_secret_scope(secret_token)


def _run_login(observed, portal, inference, **kwargs):
    state = auth_nous._nous_device_code_login(
        open_browser=False, scope="inference:invoke billing:manage", **kwargs)
    assert observed == [
        ("device", portal, "inference:invoke billing:manage"), ("poll", portal, None)]
    assert state["portal_base_url"] == portal
    assert state["inference_base_url"] == inference
    assert state["scope"] == "inference:invoke billing:manage"


@pytest.mark.parametrize(
    "mapping,portal",
    [
        ({"HERMES_PORTAL_BASE_URL": "https://worker-portal.invalid/",
          "NOUS_PORTAL_BASE_URL": "https://worker-alias.invalid/"}, "https://worker-portal.invalid"),
        ({"NOUS_PORTAL_BASE_URL": "https://worker-alias.invalid/"}, "https://worker-alias.invalid"),
    ],
    ids=["primary", "alias"],
)
def test_device_flow_uses_secondary_profile_urls(device_flow, tmp_path, mapping, portal):
    secret_scope.set_multiplex_active(True)
    token = secret_scope.set_secret_scope(
        {**mapping, "NOUS_INFERENCE_BASE_URL": "https://worker-inference.invalid/"},
        profile_home=str(tmp_path / "worker"))
    try:
        _run_login(device_flow, portal, "https://worker-inference.invalid")
    finally:
        secret_scope.reset_secret_scope(token)


def test_device_flow_retains_explicit_stored_urls(device_flow, tmp_path):
    secret_scope.set_multiplex_active(True)
    token = secret_scope.set_secret_scope(
        {"HERMES_PORTAL_BASE_URL": "https://worker-portal.invalid/",
         "NOUS_INFERENCE_BASE_URL": "https://worker-inference.invalid/"},
        profile_home=str(tmp_path / "worker"))
    try:
        _run_login(
            device_flow, "https://stored-portal.invalid", "https://stored-inference.invalid",
            portal_base_url="https://stored-portal.invalid/",
            inference_base_url="https://stored-inference.invalid/")
    finally:
        secret_scope.reset_secret_scope(token)


def test_device_flow_preserves_single_profile_environment(device_flow):
    _run_login(device_flow, "https://launch-portal.invalid", "https://launch-inference.invalid")


def test_unbound_multiplexed_device_flow_fails_before_http_client(device_flow, monkeypatch):
    secret_scope.set_multiplex_active(True)

    def forbidden_client(*args):
        pytest.fail("unbound device flow must fail before opening its HTTP client")

    monkeypatch.setattr(auth_nous, "_nous_http_client", forbidden_client)
    with pytest.raises(secret_scope.UnscopedSecretError):
        auth_nous._nous_device_code_login(open_browser=False)
    assert device_flow == []
