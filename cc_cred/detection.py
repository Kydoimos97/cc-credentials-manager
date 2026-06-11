from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Optional
import re

RATE_LIMIT_PATTERNS: list[str] = [
    "hit your limit",
    "usage allocation has been disabled",
    "rate_limit",
    "rate limit exceeded",
    "quota exceeded",
]


def parse_reset_time(text: str) -> Optional[datetime]:
    """Parse reset time from a Claude rate-limit message, returning UTC datetime or None.

    Recognises patterns like:
      "resets 8pm (America/Denver)"
      "resets 8:30pm (America/Denver)"
      "resets 8pm"

    If the parsed time is already past for today, returns tomorrow at that time.
    """
    match = re.search(
        r"resets\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*(?:\(([^)]+)\))?",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None

    time_str = match.group(1).strip().lower()
    tz_str = match.group(2)

    if ":" in time_str:
        m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)", time_str)
        if m is None:
            return None
        hour, minute, period = int(m.group(1)), int(m.group(2)), m.group(3)
    else:
        m = re.match(r"(\d{1,2})\s*(am|pm)", time_str)
        if m is None:
            return None
        hour, minute, period = int(m.group(1)), 0, m.group(2)

    if period == "pm" and hour != 12:
        hour += 12
    elif period == "am" and hour == 12:
        hour = 0

    if tz_str:
        try:
            tz = ZoneInfo(tz_str)
        except (ZoneInfoNotFoundError, KeyError):
            tz = timezone.utc
    else:
        tz = timezone.utc

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    reset_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    reset_utc = reset_local.astimezone(timezone.utc)

    if reset_utc <= now_utc:
        reset_utc += timedelta(days=1)

    return reset_utc


def is_rate_limited_text(text: str) -> bool:
    """Return True if text contains a known rate-limit pattern (case-insensitive)."""
    lower = text.lower()
    return any(pattern in lower for pattern in RATE_LIMIT_PATTERNS)


def is_rate_limited(result_msg: object, last_assistant_text: str = "") -> bool:
    """Return True if result_msg or last_assistant_text indicates a rate limit.

    Duck-typed: works with ResultMessage from claude_agent_sdk or any object
    with api_error_status and errors attributes. Safe to call with None fields.
    """
    if getattr(result_msg, "api_error_status", None) == 429:
        return True

    for error in getattr(result_msg, "errors", None) or []:
        if is_rate_limited_text(str(error)):
            return True

    if last_assistant_text and is_rate_limited_text(last_assistant_text):
        return True

    return False
