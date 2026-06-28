"""Classify account-scoped provider errors so the engine can skip retrying them
and the trace DB can record a detectable bucket.

A 401/402/403-key failure is structurally different from a flaky model or a
transient 5xx: it is account-scoped and non-retryable. This module only
classifies — it raises nothing. No I/O, no deps beyond stdlib.
"""
from __future__ import annotations

# 402 (insufficient credits) is not a distinct litellm class — it arrives as
# BadRequestError or a bare APIError — so we detect by status code first, then
# by message substring. Patterns are documented and extensible.
_CREDITS_PATTERNS = (
    "insufficient credits", "insufficient_credits", "out of credit", "402 ",
    '"code":402',
)
_AUTH_PATTERNS = ("api key", "api_key", "invalid_api_key", "revoked", "unauthorized")


def classify_provider_error(exc: Exception) -> str | None:
    """Return 'provider_auth', 'provider_credits', or None (not account-scoped).

    'provider_auth'    — 401 AuthenticationError, 403 key revocation/permission.
    'provider_credits' — 402 insufficient credits.
    None               — transient (429/5xx), content-policy 403, bad-model 400, etc.
    """
    msg = (str(exc) or "").lower()
    status = getattr(exc, "status_code", None)

    if status == 402:
        return "provider_credits"
    if status == 401:
        return "provider_auth"
    if status == 403:
        # Only a key/auth-revocation 403 is account-scoped; a content-policy
        # 403 must NOT trip an abort, so it returns None.
        return "provider_auth" if any(p in msg for p in _AUTH_PATTERNS) else None

    # Status code absent — message fallback (covers bare APIError shapes).
    if any(p in msg for p in _CREDITS_PATTERNS):
        return "provider_credits"
    if any(p in msg for p in _AUTH_PATTERNS):
        return "provider_auth"
    return None


def is_account_scoped(exc: Exception) -> bool:
    """True iff classify_provider_error buckets this as account-scoped."""
    return classify_provider_error(exc) is not None