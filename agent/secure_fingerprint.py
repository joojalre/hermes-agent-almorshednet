"""Stable SHA-3 fingerprints for non-verifier identity and cache boundaries.

These values are identifiers, not password verifiers: callers use them to keep
credentials from appearing in cache keys, telemetry, or sanitized metadata.
These values must never be used as authentication material or password
verifiers.
"""

from __future__ import annotations

import hashlib
from typing import Any


def stable_fingerprint(value: Any, *, length: int = 16) -> str:
    """Return a stable, secret-safe SHA3-256 fingerprint truncated to *length* hex chars."""
    text = "" if value is None else str(value)
    if not text:
        return ""
    digest = hashlib.sha3_256(text.encode("utf-8", errors="surrogatepass")).hexdigest()
    return digest[:length]
