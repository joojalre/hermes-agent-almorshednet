"""Stable keyed fingerprints for non-verifier identity and cache boundaries.

These values are identifiers, not password verifiers: callers use them to keep
credentials from appearing in cache keys, telemetry, or sanitized metadata.
The namespace key keeps the fingerprint format domain-separated. These values
must never be used as authentication material or password verifiers.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any


_FINGERPRINT_NAMESPACE = b"hermes-non-verifier-fingerprint-v2"


def keyed_fingerprint(value: Any, *, length: int = 16) -> str:
    """Return a stable, secret-safe HMAC fingerprint truncated to *length* hex chars."""
    text = "" if value is None else str(value)
    if not text:
        return ""
    digest = hmac.new(
        _FINGERPRINT_NAMESPACE,
        text.encode("utf-8", errors="surrogatepass"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:length]
