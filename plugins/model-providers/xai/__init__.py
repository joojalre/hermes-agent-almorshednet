"""xAI (Grok) provider profile."""

import re

from hermes_cli.version_info import get_version_info
from providers import register_provider
from providers.base import ProviderProfile

_ZERO_RPM_ALLOWANCE = re.compile(
    r"\brequests\s+per\s+minute\s*\(actual/limit\)\s*:\s*\d+\s*/\s*0(?=\s*(?:[.;,](?!\d)|$))",
    re.IGNORECASE,
)


def _classify_api_error(error, *, status_code, error_code, message, body, model):
    """A spend-tier allowance of zero cannot reopen through ordinary rate-limit backoff."""
    if status_code != 429 or error_code.lower().replace("-", "_") != "resource_exhausted":
        return None
    text = body.get("error") if isinstance(body.get("error"), str) else message
    if "historical api spend" not in text.lower() or not _ZERO_RPM_ALLOWANCE.search(text):
        return None
    # This is an account/model allowance, not proof of depleted credits. Leave configured
    # fallback policy intact, but do not retry or rotate a key for the same zero team allowance.
    return {"reason": "model_entitlement", "retryable": False, "should_fallback": True,
            "should_rotate_credential": False, "should_compress": False}


xai = ProviderProfile(
    name="xai", aliases=("grok", "x-ai", "x.ai"), api_mode="codex_responses", env_vars=("XAI_API_KEY",),
    base_url="https://api.x.ai/v1", auth_type="api_key",
    default_headers={"User-Agent": f"Hermes-Agent/{get_version_info().base_version}"},
    classify_api_error=_classify_api_error,
)

register_provider(xai)
