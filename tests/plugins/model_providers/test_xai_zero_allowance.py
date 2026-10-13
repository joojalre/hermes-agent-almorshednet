"""Explicit xAI zero RPM is an account/model allowance wall, not a busy window."""
from __future__ import annotations

import httpx
import openai
import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from providers import get_provider_profile


def _error(*, actual=0, limit=0, status=429, code="resource-exhausted", tier=True):
    message = (
        "Too many requests for team example-team and model grok-4.7. Your team's rate limit is — "
        f"Requests per Minute (actual/limit): {actual}/{limit}. "
    )
    if tier:
        message += "Your rate limit tier is determined based on your historical API spend."
    body = {"code": code, "error": message}
    response = httpx.Response(status, json=body, request=httpx.Request("POST", "https://api.x.ai/v1/responses"))
    return openai.APIStatusError(message, response=response, body=body)


def test_discovered_xai_profile_classifies_zero_allowance_without_billing_claim():
    assert get_provider_profile("xai") is not None
    result = classify_api_error(_error(), provider="xai", model="grok-4.7")
    assert result.reason is FailoverReason.model_entitlement
    assert not result.retryable and not result.should_compress
    assert not result.should_rotate_credential
    assert result.should_fallback


@pytest.mark.parametrize("provider", ["grok", "x-ai", "x.ai"])
def test_xai_aliases_use_the_same_zero_allowance_rule(provider):
    assert classify_api_error(_error(), provider=provider).reason is FailoverReason.model_entitlement


@pytest.mark.parametrize("kwargs", [
    {"actual": 0, "limit": 100},
    {"actual": 100, "limit": 100},
    {"actual": 0, "limit": "unknown"},
    {"actual": 0, "limit": 0.1},
    {"actual": 0, "limit": "0/100"},
    {"tier": False},
    {"code": "unknown"},
])
def test_unproven_zero_allowance_keeps_normal_429_recovery(kwargs):
    result = classify_api_error(_error(**kwargs), provider="xai")
    assert result.reason is FailoverReason.rate_limit
    assert result.retryable and result.should_rotate_credential and result.should_fallback


def test_other_providers_and_non_429_errors_keep_their_classification():
    assert classify_api_error(_error(), provider="openai").reason is FailoverReason.rate_limit
    assert classify_api_error(_error(status=401), provider="xai").reason is FailoverReason.auth
