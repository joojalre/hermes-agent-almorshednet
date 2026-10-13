"""The turn's explicit no-rotation verdict must fence real populated credential pools."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.agent_runtime_helpers import recover_with_credential_pool
from agent.credential_pool import CredentialPool, PooledCredential
from agent.error_classifier import FailoverReason, classify_api_error
from agent.turn_recovery import recover_after_classification
from agent.turn_retry_state import TurnRetryState


class _Error(Exception):
    def __init__(self, status, message, body):
        super().__init__(message)
        self.status_code = status
        self.body = body
        self.response = None


def _xai_error(limit):
    message = (
        "Too many requests for team example-team and model grok-4.7. "
        f"Requests per Minute (actual/limit): 0/{limit}. "
        "Your rate limit tier is determined based on your historical API spend."
    )
    return _Error(429, message, {"code": "resource-exhausted", "error": message})


def _agent(monkeypatch, tmp_path, *, provider="xai", model="grok-4.7"):
    home = tmp_path / "isolated-hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    base_url = "https://api.x.ai/v1" if provider == "xai" else "https://chatgpt.com/backend-api/codex"
    entries = [
        PooledCredential(provider=provider, id=f"entry-{i}", label=f"synthetic-{i}",
                         auth_type="api_key", priority=i, source="manual",
                         access_token=f"synthetic-not-a-real-key-{i}", base_url=base_url)
        for i in range(2)
    ]
    pool = CredentialPool(provider, entries)
    monkeypatch.setattr(pool, "_persist", Mock())
    assert pool.select(model=model).id == "entry-0"
    agent = SimpleNamespace(
        provider=provider, model=model, api_mode="codex_responses", base_url=base_url,
        api_key=entries[0].access_token, _credential_pool_entry_id="entry-0", _credential_pool=pool,
        _swap_credential=Mock(return_value=True), _get_transport=lambda: SimpleNamespace(),
    )
    agent._recover_with_credential_pool = Mock(side_effect=lambda **kw: recover_with_credential_pool(agent, **kw))
    return agent, pool


def _recover(agent, error, classified, retry):
    return recover_after_classification(
        agent, error, classified, retry, status_code=error.status_code, error_context={},
        messages=[], api_messages=[],
    )


@pytest.mark.parametrize("has_retried_429", [False, True])
def test_zero_allowance_never_benches_or_rotates_a_populated_pool(monkeypatch, tmp_path, has_retried_429):
    agent, pool = _agent(monkeypatch, tmp_path)
    error = _xai_error(0)
    classified = classify_api_error(error, provider="xai", model=agent.model)
    assert classified.reason is FailoverReason.model_entitlement and not classified.should_rotate_credential
    retry = TurnRetryState(has_retried_429=has_retried_429)
    for _ in range(2):
        result = _recover(agent, error, classified, retry)
        assert all(not entry.model_cooldowns and entry.last_status is None for entry in pool.entries())
        assert result == (False, False)
        assert retry.has_retried_429 is has_retried_429
    agent._recover_with_credential_pool.assert_not_called()
    agent._swap_credential.assert_not_called()
    pool._persist.assert_not_called()
    assert pool.current().id == "entry-0"


@pytest.mark.parametrize("hint", ["allowed", "missing", "unset"])
def test_ordinary_rate_limit_keeps_real_pool_rotation(monkeypatch, tmp_path, hint):
    agent, pool = _agent(monkeypatch, tmp_path)
    error = _xai_error(100)
    classified = classify_api_error(error, provider="xai", model=agent.model)
    assert classified.reason is FailoverReason.rate_limit and classified.should_rotate_credential
    if hint != "allowed":
        classified = SimpleNamespace(reason=FailoverReason.rate_limit, billing_unverified=False, error_context={})
        if hint == "unset":
            classified.should_rotate_credential = None
    assert _recover(agent, error, classified, TurnRetryState(has_retried_429=True)) == (True, True)
    agent._recover_with_credential_pool.assert_called_once()
    agent._swap_credential.assert_called_once()
    assert agent._swap_credential.call_args.args[0].id == "entry-1"
    assert pool.entries()[0].last_status == "exhausted"
    pool._persist.assert_called()


def test_allowed_codex_model_entitlement_still_benches_and_rotates(monkeypatch, tmp_path):
    model = "gpt-5.3-codex"
    agent, pool = _agent(monkeypatch, tmp_path, provider="openai-codex", model=model)
    message = f"The '{model}' model is not supported when using Codex with a ChatGPT account."
    error = _Error(400, message, {"detail": message})
    classified = classify_api_error(error, provider=agent.provider, model=model)
    assert classified.reason is FailoverReason.model_entitlement and classified.should_rotate_credential
    assert _recover(agent, error, classified, TurnRetryState()) == (True, True)
    agent._swap_credential.assert_called_once()
    assert agent._swap_credential.call_args.args[0].id == "entry-1"
    assert model in pool.entries()[0].model_cooldowns
    assert not pool.entries()[1].model_cooldowns
    pool._persist.assert_called()
