"""The xAI OAuth proxy's client-version refusal must end the current turn."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.error_classifier import classify_api_error
from run_agent import AIAgent


class VersionGateError(Exception):
    status_code = 426

    def __init__(self):
        super().__init__("Your Grok CLI version is outdated")
        self.body = {"error": {"message": "Your Grok CLI version is outdated"}}
        self.response = SimpleNamespace(headers={})


@pytest.mark.parametrize("provider,base_url,should_fallback", [
    ("xai-oauth", "https://cli-chat-proxy.grok.com/v1", False),
    ("xai-oauth", "https://api.x.ai/v1", True),
    ("xai", "https://cli-chat-proxy.grok.com/v1", True),
])
def test_version_gate_classification_is_limited_to_oauth_proxy(
    provider, base_url, should_fallback,
):
    verdict = classify_api_error(
        VersionGateError(), provider=provider, base_url=base_url,
    )
    assert verdict.retryable is False
    assert verdict.should_fallback is should_fallback


def test_xai_oauth_version_gate_does_not_send_a_fallback_request():
    fallback = [{
        "provider": "openrouter",
        "model": "openai/gpt-4o-mini",
        "base_url": "https://openrouter.ai/api/v1",
    }]
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="fake-test-token",
            base_url="https://cli-chat-proxy.grok.com/v1",
            provider="xai-oauth",
            model="grok-4.7",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback,
        )
        agent.client = MagicMock()

    calls = []

    def fake_api_call(_api_kwargs):
        calls.append((agent.provider, agent.model))
        if len(calls) == 1:
            raise VersionGateError()
        message = SimpleNamespace(content="unexpected fallback", tool_calls=None)
        choice = SimpleNamespace(message=message, finish_reason="stop")
        return SimpleNamespace(choices=[choice], model=agent.model, usage=None)

    fallback_client = MagicMock()
    fallback_client.api_key = "fake-fallback-token"
    fallback_client.base_url = "https://openrouter.ai/api/v1"
    fallback_client._custom_headers = None
    fallback_client.default_headers = None

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(fallback_client, "openai/gpt-4o-mini")) as resolve,
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda model, provider: model),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        result = agent.run_conversation("test request")

    assert calls == [("xai-oauth", "grok-4.7")]
    resolve.assert_not_called()
    assert result["completed"] is False
    assert result["failure_retryable"] is False
