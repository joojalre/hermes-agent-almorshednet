"""OAuth error hooks share a missing profile's family without changing auth identity."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

import providers
from agent.error_classifier import FailoverReason, classify_api_error
from providers.base import ProviderProfile


class _AllowanceError(Exception):
    status_code = 429
    response = None

    def __init__(self, limit=0):
        text = (
            "Too many requests for team example-team and model grok-4.7. "
            f"Requests per Minute (actual/limit): 0/{limit}. "
            "Your rate limit tier is determined based on your historical API spend."
        )
        super().__init__(text)
        self.body = {"code": "resource-exhausted", "error": text}


def _answer():
    message = SimpleNamespace(content="fallback answered", tool_calls=None, reasoning=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None)


def _loop_agent(monkeypatch, *, fallback):
    from run_agent import AIAgent

    # Replace only external clients; run the actual turn, classifier, recovery and fallback binder.
    monkeypatch.setattr(httpx.Client, "send", Mock(side_effect=AssertionError("no network in this test")))
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kw: [])
    monkeypatch.setattr("model_tools.check_toolset_requirements", lambda *a, **kw: {})
    monkeypatch.setattr(AIAgent, "_create_openai_client", lambda *a, **kw: Mock())
    agent = AIAgent(
        provider="xai-oauth", api_mode="codex_responses", model="grok-4.7",
        api_key="synthetic-not-a-real-token", base_url="https://api.x.ai/v1",
        quiet_mode=True, skip_context_files=True, skip_memory=True, enabled_toolsets=[],
        fallback_model=({"provider": "custom", "model": "fallback-model",
                         "base_url": "https://example.invalid/v1", "api_key": "synthetic"} if fallback else None),
    )
    agent._disable_streaming = True
    agent._cached_system_prompt = "Stable synthetic prompt."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._api_max_retries = 3
    agent._try_recover_primary_transport = Mock(return_value=False)
    monkeypatch.setattr("agent.auxiliary_client.resolve_provider_client", lambda *a, **kw: (
        Mock(base_url="https://example.invalid/v1", api_key="synthetic",
             _custom_headers={}, default_headers={}), "fallback-model"))
    return agent


@pytest.mark.parametrize("fallback", [False, True], ids=["no-fallback", "explicit-fallback"])
def test_oauth_zero_allowance_full_turn_does_not_repeat_or_wait(monkeypatch, fallback):
    agent = _loop_agent(monkeypatch, fallback=fallback)
    calls = []

    def call(_kwargs):
        calls.append((agent.provider, agent.model))
        if agent.provider == "xai-oauth":
            raise _AllowanceError()
        return _answer()

    agent._interruptible_api_call = call
    wait = Mock(return_value=None)
    monkeypatch.setattr("agent.turn_api_error.interruptible_backoff_sleep", wait)
    try:
        result = agent.run_conversation("synthetic probe", conversation_history=[], task_id="oauth-zero")
    finally:
        agent.close()
    assert calls.count(("xai-oauth", "grok-4.7")) == 1
    wait.assert_not_called()
    if fallback:
        assert calls == [("xai-oauth", "grok-4.7"), ("custom", "fallback-model")]
        assert result["completed"] and result["final_response"] == "fallback answered"
    else:
        assert agent.provider == "xai-oauth"
        assert result["failed"] and result["failure_reason"] == "model_entitlement"
        assert result["failure_retryable"] is False


def test_ordinary_oauth_429_full_turn_keeps_backoff(monkeypatch):
    agent = _loop_agent(monkeypatch, fallback=False)
    calls = Mock(side_effect=_AllowanceError(100))
    agent._interruptible_api_call = calls
    wait = Mock(return_value=None)
    monkeypatch.setattr("agent.turn_api_error.interruptible_backoff_sleep", wait)
    try:
        result = agent.run_conversation("synthetic probe", conversation_history=[], task_id="oauth-rate")
    finally:
        agent.close()
    assert calls.call_count == 3
    assert wait.call_count == 2
    assert agent.provider == "xai-oauth"
    assert result["failed"] and result["failure_reason"] == "rate_limit"


def test_oauth_zero_allowance_classification_preserves_identity():
    verdict = classify_api_error(_AllowanceError(), provider="xai-oauth", model="grok-4.7")
    assert verdict.provider == "xai-oauth"
    assert verdict.reason is FailoverReason.model_entitlement
    assert not verdict.retryable and not verdict.should_rotate_credential
    assert verdict.should_fallback and not verdict.should_compress


@pytest.mark.parametrize("exact_hook", ["missing", "declines", "overloaded"])
def test_exact_oauth_profile_wins_even_when_it_has_no_hook(monkeypatch, exact_hook):
    providers.get_provider_profile("xai")  # Finish bundled discovery before adding test profiles.
    family_hook = Mock(return_value={"reason": "model_entitlement", "retryable": False})
    own_hook = None if exact_hook == "missing" else Mock(
        return_value={"reason": "overloaded", "retryable": True} if exact_hook == "overloaded" else None)
    monkeypatch.setitem(providers._REGISTRY, "synthetic-family", ProviderProfile(
        name="synthetic-family", classify_api_error=family_hook))
    monkeypatch.setitem(providers._REGISTRY, "synthetic-family-oauth", ProviderProfile(
        name="synthetic-family-oauth", auth_type="oauth_external", classify_api_error=own_hook))
    verdict = classify_api_error(_AllowanceError(), provider="synthetic-family-oauth")
    assert verdict.reason is (FailoverReason.overloaded if exact_hook == "overloaded" else FailoverReason.rate_limit)
    family_hook.assert_not_called()
    if own_hook:
        own_hook.assert_called_once()


def test_missing_oauth_family_keeps_generic_rate_limit():
    verdict = classify_api_error(_AllowanceError(), provider="synthetic-unknown-family-oauth")
    assert verdict.reason is FailoverReason.rate_limit and verdict.retryable


def test_ordinary_oauth_429_keeps_rotation_and_retry_hints():
    verdict = classify_api_error(_AllowanceError(100), provider="xai-oauth")
    assert verdict.reason is FailoverReason.rate_limit
    assert verdict.retryable and verdict.should_rotate_credential and verdict.should_fallback
