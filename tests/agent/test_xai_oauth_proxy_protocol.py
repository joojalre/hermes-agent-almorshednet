"""Official xAI OAuth proxy requests use session auth and the current wire model."""
from __future__ import annotations

import json
from unittest.mock import Mock

import httpx
import pytest
from openai import OpenAI

from hermes_cli import auth_constants, auth_xai, providers, runtime_provider

PROXY = "https://cli-chat-proxy.grok.com/v1"
API = "https://api.x.ai/v1"
KEY = "synthetic-not-a-real-token"
MESSAGES = [{"role": "user", "content": "synthetic request"}]


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    original = httpx.Client.send

    def local_only(client, request, **kwargs):
        assert isinstance(client._transport, httpx.MockTransport), "external HTTP forbidden"
        return original(client, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", local_only)
    monkeypatch.setattr(httpx.AsyncClient, "send", Mock(side_effect=AssertionError("external HTTP forbidden")))


def _sdk(kwargs, captured):
    def receive(request):
        body = json.loads(request.content)
        captured.append((request, body))
        response = {"id": "resp_synthetic", "object": "response", "created_at": 0,
                    "status": "completed", "model": body["model"], "output": []}
        return httpx.Response(200, json=response)

    return OpenAI(api_key=kwargs["api_key"], base_url=kwargs["base_url"], max_retries=0,
                  default_headers=kwargs.get("default_headers"),
                  http_client=httpx.Client(transport=httpx.MockTransport(receive)))


def _primary(monkeypatch, provider, base, captured):
    from run_agent import AIAgent

    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kw: [])
    monkeypatch.setattr("model_tools.check_toolset_requirements", lambda *a, **kw: {})
    monkeypatch.setattr(AIAgent, "_create_openai_client",
                        lambda self, kwargs, **kw: _sdk(kwargs, captured))
    return AIAgent(provider=provider, api_mode="codex_responses", model="grok-4.7",
                   api_key=KEY, base_url=base, quiet_mode=True, skip_memory=True,
                   skip_context_files=True, enabled_toolsets=[])


def _primary_request(agent, model, extra_headers=None):
    from agent.transports.codex import ResponsesApiTransport

    kwargs = ResponsesApiTransport().build_kwargs(
        model, MESSAGES, provider=agent.provider, base_url=agent.base_url,
        is_xai_responses=agent.provider in {"xai", "xai-oauth"},
        request_overrides={"extra_headers": extra_headers or {}})
    for key in tuple(kwargs):
        if key.startswith("_"):
            kwargs.pop(key)
    return agent.client.responses.create(**kwargs)


def test_oauth_default_resolver_uses_proxy_without_auth_io(monkeypatch):
    monkeypatch.setattr(runtime_provider, "resolve_xai_oauth_runtime_credentials", lambda: {
        "api_key": KEY, "base_url": auth_constants.DEFAULT_XAI_OAUTH_BASE_URL})
    result = runtime_provider._resolve_oauth_runtime("xai-oauth", "xai-oauth", {}, "grok-4.7")
    assert result["provider"] == "xai-oauth"
    assert result["api_mode"] == "codex_responses"
    assert result["base_url"] == PROXY
    assert providers.HERMES_OVERLAYS["xai-oauth"].base_url_override == PROXY
    assert providers.HERMES_OVERLAYS["xai"].base_url_override == API


@pytest.mark.parametrize("base", [PROXY, PROXY + "/"])
def test_inference_accepts_exact_canonical_proxy(base):
    assert auth_xai._xai_validate_inference_base_url(base, fallback=API) == PROXY


@pytest.mark.parametrize("base", [
    "http://cli-chat-proxy.grok.com/v1", "https://cli-chat-proxy.grok.com:443/v1",
    "https://user@cli-chat-proxy.grok.com/v1", "https://user:pass@cli-chat-proxy.grok.com/v1",
    PROXY + "?x=1", PROXY + "?", PROXY + "#x", PROXY + "#",
    "https://cli-chat-proxy.grok.com.evil.invalid/v1", "https://evil.cli-chat-proxy.grok.com/v1",
    "https://cli-chat-proxy.grok.com./v1", "https://cli-chat-proxy.grok.com/v2",
    "https://cli-chat-proxy.grok.com/v1/responses", "https://cli-chat-proxy.grok.com",
])
def test_inference_rejects_noncanonical_proxy(base):
    assert auth_xai._xai_validate_inference_base_url(base, fallback=API) == API


@pytest.mark.parametrize("field", ["token_endpoint", "device_authorization_endpoint", "issuer"])
def test_proxy_never_becomes_oauth_credential_endpoint(field):
    with pytest.raises(auth_constants.AuthError):
        auth_xai._xai_validate_oauth_endpoint(PROXY, field=field)
    endpoint = "https://auth.x.ai/oauth2/token"
    assert auth_xai._xai_validate_oauth_endpoint(endpoint, field=field) == endpoint


def test_explicit_api_override_and_login_contract_stay_intact():
    assert auth_xai._xai_validate_inference_base_url(API, fallback=PROXY) == API
    assert auth_constants.XAI_OAUTH_ISSUER == "https://auth.x.ai"
    assert auth_constants.XAI_OAUTH_CLIENT_ID == "b1a00492-073a-47ea-816f-4c329264a828"
    assert auth_constants.XAI_OAUTH_SCOPE == "openid profile email offline_access grok-cli:access api:access"


def test_primary_sdk_proxy_request_follows_current_model(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model": {"default_headers": {
        "X-Grok-Model-Override": "stale-client-model", "X-Xai-Token-Auth": "stale-client-auth"}}})
    captured = []
    agent = _primary(monkeypatch, "xai-oauth", PROXY, captured)
    try:
        for model, header in [("grok-4.7", "x-grok-model-override"),
                              ("grok-4.6", "X-Grok-Model-Override")]:
            _primary_request(agent, model, {header: "stale-model", "X-Xai-Token-Auth": "stale-auth"})
    finally:
        agent.close()
    assert [body["model"] for _, body in captured] == ["grok-4.7", "grok-4.6"]
    for request, body in captured:
        assert str(request.url) == PROXY + "/responses"
        assert request.headers["Authorization"] == "Bearer " + KEY
        assert request.headers.get("X-XAI-Token-Auth") == "xai-grok-cli"
        assert request.headers.get("x-grok-model-override") == body["model"]
        assert request.headers.get_list("x-grok-model-override") == [body["model"]]
        assert request.headers.get_list("x-xai-token-auth") == ["xai-grok-cli"]
        assert request.headers["User-Agent"].startswith("Hermes-Agent/")
        assert "x-grok-client-version" not in request.headers
        assert "x-grok-user-id" not in request.headers


@pytest.mark.parametrize("provider,base", [
    ("xai", API), ("xai-oauth", API), ("custom", "https://example.invalid/v1"),
    ("xai", PROXY),
    ("xai-oauth", "https://cli-chat-proxy.grok.com:443/v1"),
    ("xai-oauth", "https://user@cli-chat-proxy.grok.com/v1"),
    ("xai-oauth", "https://cli-chat-proxy.grok.com/v1?x=1"),
    ("xai-oauth", "https://cli-chat-proxy.grok.com/v1#x"),
    ("xai-oauth", "https://cli-chat-proxy.grok.com.evil.invalid/v1"),
    ("xai-oauth", "http://cli-chat-proxy.grok.com/v1"),
])
def test_primary_api_key_and_custom_routes_have_no_proxy_marker(monkeypatch, provider, base):
    captured = []
    try:
        agent = _primary(monkeypatch, provider, base, captured)
    except ValueError as exc:
        assert provider == "xai-oauth"
        assert str(exc) == "xAI OAuth proxy requires its canonical HTTPS /v1 URL"
        assert not captured
        return
    try:
        _primary_request(agent, "grok-4.7")
    finally:
        agent.close()
    request, _ = captured[0]
    assert "X-XAI-Token-Auth" not in request.headers
    assert "x-grok-model-override" not in request.headers


def test_auxiliary_sdk_proxy_request_follows_current_model(monkeypatch):
    from agent import auxiliary_client as aux

    captured = []
    monkeypatch.setattr(aux, "_resolve_xai_oauth_for_aux", lambda: (KEY, PROXY))
    monkeypatch.setattr(aux, "_create_openai_client", lambda **kw: _sdk(kw, captured))
    client, _ = aux._build_xai_oauth_aux_client("grok-4.7")
    try:
        for model, header in [("grok-4.7", "x-grok-model-override"),
                              ("grok-4.6", "X-Grok-Model-Override")]:
            kwargs, _, _ = client.chat.completions._build_responses_kwargs({
                "model": model, "messages": MESSAGES,
                "extra_headers": {header: "stale-model", "X-Xai-Token-Auth": "stale-auth"}})
            for key in tuple(kwargs):
                if key.startswith("_"):
                    kwargs.pop(key)
            client._real_client.responses.create(**kwargs)
    finally:
        client.close()
    for request, body in captured:
        assert str(request.url) == PROXY + "/responses"
        assert request.headers.get("X-XAI-Token-Auth") == "xai-grok-cli"
        assert request.headers.get("x-grok-model-override") == body["model"]
        assert request.headers["User-Agent"].startswith("Hermes-Agent/")
    assert [body["model"] for _, body in captured] == ["grok-4.7", "grok-4.6"]


def test_auxiliary_proxy_pool_and_refresh_identity_are_exact():
    from agent import auxiliary_client as aux

    for table in [aux._POOL_PROVIDER_BY_HOST, aux._AUTH_REFRESH_PROVIDER_BY_HOST]:
        assert aux._provider_for_host(PROXY, table) == "xai-oauth"
        for base in ["https://cli-chat-proxy.grok.com.evil.invalid/v1", PROXY + "?x=1",
                     "https://cli-chat-proxy.grok.com:443/v1", "https://user@cli-chat-proxy.grok.com/v1"]:
            assert aux._provider_for_host(base, table) is None


def test_auxiliary_proxy_keeps_xai_responses_issuer_and_tier_policy(monkeypatch):
    from agent import auxiliary_client as aux

    monkeypatch.setattr(aux, "_resolve_xai_oauth_for_aux", lambda: (KEY, PROXY))
    monkeypatch.setattr(aux, "_create_openai_client", lambda **kw: _sdk(kw, []))
    client, _ = aux._build_xai_oauth_aux_client("grok-4.7")
    tools = [{"type": "function", "function": {"name": "synthetic_tool",
             "parameters": {"type": "object", "properties": {
                 "name": {"type": "string", "pattern": ".*", "format": "email"}}}}}]
    try:
        kwargs, _, _ = client.chat.completions._build_responses_kwargs({
            "messages": MESSAGES, "tools": tools, "extra_body": {"service_tier": "priority"}})
    finally:
        client.close()
    assert kwargs["_issuer_kind"] == "xai_responses"
    assert "service_tier" not in kwargs
    property_schema = kwargs["tools"][0]["parameters"]["properties"]["name"]
    assert "pattern" not in property_schema and "format" not in property_schema
    assert tools[0]["function"]["parameters"]["properties"]["name"]["pattern"] == ".*"


@pytest.mark.parametrize("provider", [None, "xai", "custom"])
def test_auxiliary_proxy_url_alone_never_selects_oauth_auth_mode(provider):
    from agent.auxiliary_client import CodexAuxiliaryClient

    captured = []
    real_client = _sdk({"api_key": KEY, "base_url": PROXY}, captured)
    if provider is not None:
        real_client._hermes_aux_effective_provider = provider
    client = CodexAuxiliaryClient(real_client, "grok-4.7")
    try:
        kwargs, _, _ = client.chat.completions._build_responses_kwargs({"messages": MESSAGES})
        assert "extra_headers" not in kwargs
        for key in tuple(kwargs):
            if key.startswith("_"):
                kwargs.pop(key)
        real_client.responses.create(**kwargs)
    finally:
        client.close()
    request, _ = captured[0]
    assert "x-xai-token-auth" not in request.headers
    assert "x-grok-model-override" not in request.headers


def test_primary_recreated_client_keeps_single_current_proxy_headers(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model": {"default_headers": {
        "X-Grok-Model-Override": "stale-client-model", "X-Xai-Token-Auth": "stale-client-auth"}}})
    captured = []
    agent = _primary(monkeypatch, "xai-oauth", PROXY, captured)
    try:
        agent._apply_client_headers_for_base_url(PROXY)
        agent._replace_primary_openai_client(reason="synthetic_rebuild")
        _primary_request(agent, "grok-4.6")
    finally:
        agent.close()
    request, body = captured[0]
    assert request.headers.get_list("x-grok-model-override") == [body["model"]]
    assert request.headers.get_list("x-xai-token-auth") == ["xai-grok-cli"]


@pytest.mark.parametrize("base", [API, "https://example.invalid/v1", PROXY])
def test_existing_generic_models_get_has_no_oauth_marker(monkeypatch, base):
    from hermes_cli import models

    calls = []

    def receive(url, **kwargs):
        calls.append((url, kwargs.get("headers") or {}))
        return {"data": [{"id": "grok-4.7"}]}

    monkeypatch.setattr(models, "_get_json", receive)
    result = models.probe_api_models(KEY, base, api_mode="codex_responses")
    assert result["models"] == ["grok-4.7"]
    assert calls[0][0] == base + "/models"
    assert "X-XAI-Token-Auth" not in calls[0][1]
    assert "x-grok-model-override" not in calls[0][1]
