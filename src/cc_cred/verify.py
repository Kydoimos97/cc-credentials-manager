"""Token verification against the Anthropic API."""
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

VERIFY_URL = "https://api.anthropic.com/v1/models"
VERIFY_HEADERS = {"anthropic-version": "2023-06-01"}
VERIFY_TIMEOUT = 8.0
CACHE_TTL_SECONDS = 300  # re-verify at most once per 5 minutes


def check_token(token: str) -> tuple[str, Optional[str]]:
    """Verify a token against the Anthropic API.

    Returns (status, error_message) where status is one of:
      "available"  — token authenticated successfully
      "invalid"    — token rejected (401/403)
      "unknown"    — could not reach the API or unexpected response
    """
    try:
        resp = httpx.get(
            VERIFY_URL,
            headers={**VERIFY_HEADERS, "Authorization": f"Bearer {token}"},
            timeout=VERIFY_TIMEOUT,
        )
        if resp.status_code == 200:
            return "available", None
        if resp.status_code in (401, 403):
            return "invalid", f"API returned {resp.status_code}"
        return "unknown", f"Unexpected status {resp.status_code}"
    except httpx.TimeoutException:
        return "unknown", "Request timed out"
    except httpx.RequestError as exc:
        return "unknown", str(exc)


def should_reverify(last_checked: Optional[str]) -> bool:
    """Return True if enough time has passed to warrant a fresh check."""
    if last_checked is None:
        return True
    try:
        checked_at = datetime.fromisoformat(last_checked)
        age = datetime.now(timezone.utc) - checked_at
        return age > timedelta(seconds=CACHE_TTL_SECONDS)
    except ValueError:
        return True
