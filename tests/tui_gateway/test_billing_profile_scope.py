"""Billing RPCs resolve state and credentials for the requested profile.

The registered handlers run against real temporary profile homes. Only the
view/request seams are replaced, so no account, login, or payment is contacted.
"""

from importlib import import_module
from pathlib import Path

import pytest

from agent import secret_scope
from hermes_constants import (
    get_hermes_home,
    get_hermes_home_override,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from tools.terminal_scope import get_terminal_scope, reset_terminal_scope, set_terminal_scope
from tui_gateway import launch_profile_policy, server


MARKER = "BILLING_SCOPE_MARKER"
ROUTES = [
    ("billing.state", "agent.billing_view", "build_billing_state", {}),
    ("usage.bars", "agent.billing_usage", "build_usage_model", {}),
    ("subscription.state", "agent.subscription_view", "build_subscription_state", {}),
    ("subscription.preview", "hermes_cli.nous_billing", "post_subscription_preview",
     {"subscription_type_id": "synthetic-tier"}),
    ("subscription.change", "hermes_cli.nous_billing", "put_subscription_pending_change",
     {"cancel": True}),
    ("subscription.resume", "hermes_cli.nous_billing", "delete_subscription_pending_change", {}),
    ("subscription.upgrade", "hermes_cli.nous_billing", "post_subscription_upgrade",
     {"subscription_type_id": "synthetic-tier", "idempotency_key": "synthetic-operation"}),
    ("billing.charge", "hermes_cli.nous_billing", "post_charge",
     {"amount_usd": "10", "idempotency_key": "synthetic-operation"}),
    ("billing.charge_status", "hermes_cli.nous_billing", "get_charge_status",
     {"charge_id": "synthetic-charge"}),
    ("billing.auto_reload", "hermes_cli.nous_billing", "patch_auto_top_up",
     {"enabled": True, "threshold": "5", "top_up_amount": "10"}),
    ("billing.step_up", "hermes_cli.auth", "step_up_nous_billing_scope", {}),
]


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    launch = tmp_path / "launch"
    worker = launch / "profiles" / "worker"
    for home, value in ((launch, "synthetic-launch"), (worker, "synthetic-worker")):
        home.mkdir(parents=True)
        (home / ".env").write_text(f"{MARKER}={value}\n", encoding="utf-8")
        (home / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv(MARKER, "synthetic-ambient")
    monkeypatch.setattr(server, "_hermes_home", launch)
    monkeypatch.setattr(server, "_served_profile_homes", set())
    monkeypatch.setattr(launch_profile_policy, "_snapshot", None)

    def forbidden_request(*args, **kwargs):
        pytest.fail("billing scope test must not perform a network request")

    import httpx
    import urllib.request
    from hermes_cli import anon_auth

    monkeypatch.setattr(httpx.Client, "request", forbidden_request)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_request)
    monkeypatch.setattr(anon_auth, "has_free_tier_account", lambda: False)
    home_token = set_hermes_home_override(None)
    secret_token = secret_scope.set_secret_scope(None)
    terminal_token = set_terminal_scope(None)
    launch_profile_policy.activate_multi_profile_hosting()
    try:
        yield launch, worker
    finally:
        secret_scope.set_multiplex_active(False)
        reset_terminal_scope(terminal_token)
        secret_scope.reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


def _synthetic_response(method):
    from agent.billing_usage import UsageModel
    from agent.billing_view import BillingState
    from agent.subscription_view import SubscriptionState

    return {
        "billing.state": BillingState(logged_in=True),
        "usage.bars": UsageModel(available=True),
        "subscription.state": SubscriptionState(logged_in=True),
        "subscription.preview": {"effect": "no_op"},
        "subscription.change": {"message": "synthetic change"},
        "subscription.resume": {"message": "synthetic resume"},
        "subscription.upgrade": {"status": "completed"},
        "billing.charge": {"chargeId": "synthetic-charge"},
        "billing.charge_status": {"status": "pending"},
        "billing.auto_reload": {},
        "billing.step_up": True,
    }[method]


def _capture_client(monkeypatch, module, function, response, observed, *, fail=False):
    def client(*args, **kwargs):
        try:
            marker = secret_scope.get_secret(MARKER)
        except secret_scope.UnscopedSecretError:
            marker = None
        observed.append((get_hermes_home(), marker))
        if fail:
            raise RuntimeError("synthetic client failure")
        return response

    monkeypatch.setattr(import_module(module), function, client)


def _assert_unbound(launch):
    assert get_hermes_home() == launch
    assert get_hermes_home_override() is None
    assert secret_scope.current_secret_scope() is None
    assert get_terminal_scope() is None
    assert server.os.environ["HERMES_HOME"] == str(launch)
    assert server.os.environ[MARKER] == "synthetic-ambient"


@pytest.mark.parametrize("method,module,function,params", ROUTES, ids=[r[0] for r in ROUTES])
@pytest.mark.parametrize("profile", ["worker", "default", None], ids=["secondary", "default", "omitted"])
def test_registered_billing_handler_resolves_requested_profile(
    hosted, monkeypatch, method, module, function, params, profile
):
    launch, worker = hosted
    expected = (worker, "synthetic-worker") if profile == "worker" else (launch, "synthetic-launch")
    observed = []
    _capture_client(monkeypatch, module, function, _synthetic_response(method), observed)
    request = dict(params)
    if profile is not None:
        request["profile"] = profile

    response = server._methods[method]("synthetic-rpc", request)

    assert observed == [expected], f"{method} resolved the wrong home or secret scope"
    assert response["result"]["ok"] is True
    _assert_unbound(launch)


@pytest.mark.parametrize("method,module,function,params", ROUTES, ids=[r[0] for r in ROUTES])
def test_registered_billing_handler_releases_scope_after_client_error(
    hosted, monkeypatch, method, module, function, params
):
    launch, worker = hosted
    observed = []
    _capture_client(monkeypatch, module, function, None, observed, fail=True)

    response = server._methods[method]("synthetic-rpc-error", {**params, "profile": "worker"})

    assert observed == [(worker, "synthetic-worker")]
    assert "result" in response
    _assert_unbound(launch)
