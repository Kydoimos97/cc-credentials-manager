"""Token verification against the Anthropic API."""
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from cc_cred._logging import get_logger, mask_token

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
    log = get_logger()
    masked = mask_token(token)

    log.debug(f"check_token  token={masked}  url={VERIFY_URL}  timeout={VERIFY_TIMEOUT}s")

    try:
        resp = httpx.get(
            VERIFY_URL,
            headers={**VERIFY_HEADERS, "Authorization": f"Bearer {token}"},
            timeout=VERIFY_TIMEOUT,
        )

        body_preview = resp.text[:2000] if resp.text else "(empty)"
        log.debug(
            f"check_token response  status={resp.status_code}"
            f"  headers={dict(resp.headers)}"
            f"  body={body_preview!r}"
        )

        if resp.status_code == 200:
            log.debug(f"check_token → available  token={masked}")
            return "available", None

        if resp.status_code in (401, 403):
            log.debug(f"check_token → rejected  token={masked}  status={resp.status_code}  body={resp.text[:500]!r}")
            return "invalid", f"API returned {resp.status_code}"

        log.debug(f"check_token → unexpected  token={masked}  status={resp.status_code}  body={resp.text[:500]!r}")
        return "unknown", f"Unexpected status {resp.status_code}"

    except httpx.TimeoutException as exc:
        log.debug(f"check_token timed out  token={masked}  url={VERIFY_URL}  error={exc}")
        return "unknown", "Request timed out"

    except httpx.RequestError as exc:
        log.debug(f"check_token request error  token={masked}  type={type(exc).__name__}  error={exc}")
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
