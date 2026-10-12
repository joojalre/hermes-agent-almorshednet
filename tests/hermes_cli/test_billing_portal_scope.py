"""Billing portal routing must not borrow a sibling profile's ambient override."""

import pytest

from agent import secret_scope
from hermes_cli.nous_billing import resolve_portal_base_url


@pytest.fixture
def multiplexed(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PORTAL_BASE_URL", "https://launch.invalid/")
    monkeypatch.setenv("NOUS_PORTAL_BASE_URL", "https://launch-alias.invalid/")
    token = secret_scope.set_secret_scope(None)
    secret_scope.set_multiplex_active(True)
    try:
        yield
    finally:
        secret_scope.set_multiplex_active(False)
        secret_scope.reset_secret_scope(token)


@pytest.mark.parametrize(
    "mapping,expected",
    [
        ({"HERMES_PORTAL_BASE_URL": "https://worker.invalid/", "NOUS_PORTAL_BASE_URL": "https://alias.invalid/"},
         "https://worker.invalid"),
        ({"NOUS_PORTAL_BASE_URL": "https://worker-alias.invalid/"}, "https://worker-alias.invalid"),
    ],
    ids=["primary-precedence", "alias-only"],
)
def test_scoped_portal_override_wins_over_ambient_launch(multiplexed, tmp_path, mapping, expected):
    token = secret_scope.set_secret_scope(mapping, profile_home=str(tmp_path / "worker"))
    try:
        assert resolve_portal_base_url({"portal_base_url": "https://stored.invalid/"}) == expected
    finally:
        secret_scope.reset_secret_scope(token)


def test_scope_without_portal_override_uses_own_stored_state(multiplexed, tmp_path):
    token = secret_scope.set_secret_scope({}, profile_home=str(tmp_path / "worker"))
    try:
        assert resolve_portal_base_url({"portal_base_url": "https://worker-stored.invalid/"}) == "https://worker-stored.invalid"
    finally:
        secret_scope.reset_secret_scope(token)


def test_unscoped_multiplexed_portal_lookup_fails_closed(multiplexed):
    with pytest.raises(secret_scope.UnscopedSecretError):
        resolve_portal_base_url({"portal_base_url": "https://stored.invalid/"})
