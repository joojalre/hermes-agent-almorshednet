"""Credential-pool recovery gated by the classifier's rotation policy."""

from __future__ import annotations

from typing import Any


def _recover_with_pool_if_allowed(
    agent: Any, classified: Any, *, status_code: int | None,
    has_retried_429: bool, error_context: dict,
) -> tuple[bool, bool]:
    """Fence pool mutation on an explicit veto; preserve unset hints and retry state."""
    if getattr(classified, "should_rotate_credential", True) is False:
        return False, has_retried_429
    return agent._recover_with_credential_pool(
        status_code=status_code, has_retried_429=has_retried_429,
        classified_reason=classified.reason, error_context=error_context,
        billing_unverified=classified.billing_unverified,
    )
