"""A zero xAI model allowance ends the attempt or follows the configured fallback immediately."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from agent.turn_api_error import settle_unrecovered_error
from agent.turn_retry_state import TurnRetryState


class _Error(Exception):
    status_code = 429
    response = None

    def __init__(self):
        message = (
            "Too many requests for team example-team and model grok-4.7. "
            "Requests per Minute (actual/limit): 0/0. "
            "Your rate limit tier is determined based on your historical API spend."
        )
        super().__init__(message)
        self.body = {"code": "resource-exhausted", "error": message}


@pytest.mark.parametrize("fallback", [False, True])
def test_zero_allowance_settlement_does_not_wait_or_repeat_same_request(monkeypatch, fallback):
    error = _Error()
    classified = classify_api_error(error, provider="xai", model="grok-4.7")
    agent = SimpleNamespace(
        provider="xai", model="grok-4.7", log_prefix="", _credential_pool=None,
        _has_pending_fallback=Mock(return_value=fallback),
        _try_activate_fallback=Mock(return_value=fallback),
        _buffer_diagnostic_status=Mock(),
    )
    terminal = Mock(return_value={"failed": True, "failure_reason": classified.reason.value})
    wait = Mock(side_effect=AssertionError("zero allowance must not wait for a rate-limit reset"))
    monkeypatch.setattr("agent.conversation_loop._is_copilot_provider", lambda a: False)
    monkeypatch.setattr("agent.conversation_loop._arm_fallback_restart", lambda a, m, s, r: s)
    monkeypatch.setattr("agent.turn_api_error.settle_delivered_partial", lambda *a: None)
    monkeypatch.setattr("agent.turn_api_error.nonretryable_client_error_result", terminal)
    monkeypatch.setattr("agent.turn_recovery.interruptible_backoff_sleep", wait)
    verdict = settle_unrecovered_error(
        agent, api_error=error, classified=classified, _retry=TurnRetryState(), status_code=429,
        error_msg=str(error).lower(), is_context_length_error=False,
        is_rate_limited=classified.reason is FailoverReason.rate_limit, _is_zai_coding_overload=False,
        _provider="xai", _base="https://api.x.ai/v1", _model="grok-4.7",
        messages=[], api_messages=[], api_kwargs={}, active_system_prompt="stable prompt",
        conversation_history=None, approx_tokens=0, retry_count=1, max_retries=3,
        compression_attempts=0, api_call_count=1,
    )
    assert classified.reason is FailoverReason.model_entitlement
    assert verdict.action == ("break" if fallback else "return")
    assert verdict.active_system_prompt == "stable prompt"
    agent._try_activate_fallback.assert_called_once_with(reason=FailoverReason.model_entitlement, reset_at=None)
    assert terminal.call_count == (0 if fallback else 1)
    wait.assert_not_called()
