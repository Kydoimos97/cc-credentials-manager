"""Read live session state from ~/.ccode/states/sessions/ and fall back to
transcript JSONL parsing when the state file is absent."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cc_cred._logging import get_logger

SESSION_STATE_DIR = Path.home() / ".ccode" / "states" / "sessions"


@dataclass
class SessionUsage:
    """Normalised usage snapshot extracted from either source."""
    session_id: str
    cost_usd: Optional[float]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    # Unix timestamp → datetime, from rate_limits.five_hour.resets_at
    rate_limit_resets_at: Optional[datetime]
    rate_limit_used_pct: Optional[float]
    source: str  # "state_file" | "transcript" | "none"


def read_session_state(session_id: str) -> Optional[dict]:
    """Return the raw dict from ~/.ccode/states/sessions/{session_id}.jsonl or None."""
    log = get_logger()
    path = SESSION_STATE_DIR / f"{session_id}.jsonl"
    log.debug(f"read_session_state  path={path}  exists={path.exists()}")
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.debug(f"read_session_state failed  error={exc}")
        return None


def _resets_at_to_datetime(unix_ts: Optional[int]) -> Optional[datetime]:
    if not unix_ts or unix_ts <= 0:
        return None
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc)


def extract_from_state(session_id: str, state: dict) -> SessionUsage:
    """Pull usage fields from a live session state dict."""
    cost_data = state.get("cost") or {}
    ctx = state.get("context_window") or {}
    rl = state.get("rate_limits") or {}
    five_hour = rl.get("five_hour") or {}

    return SessionUsage(
        session_id=session_id,
        cost_usd=cost_data.get("total_cost_usd"),
        input_tokens=ctx.get("total_input_tokens"),
        output_tokens=ctx.get("total_output_tokens"),
        rate_limit_resets_at=_resets_at_to_datetime(five_hour.get("resets_at")),
        rate_limit_used_pct=five_hour.get("used_percentage"),
        source="state_file",
    )


def parse_transcript(session_id: str, transcript_path: str) -> SessionUsage:
    """Sum token usage from transcript JSONL assistant messages.

    No cost data is available from the transcript alone.
    """
    log = get_logger()
    path = Path(transcript_path)
    log.debug(f"parse_transcript fallback  path={path}")

    input_tokens = 0
    output_tokens = 0

    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "assistant":
                    continue
                usage = (record.get("message") or {}).get("usage") or {}
                input_tokens += usage.get("input_tokens", 0)
                input_tokens += usage.get("cache_creation_input_tokens", 0)
                input_tokens += usage.get("cache_read_input_tokens", 0)
                output_tokens += usage.get("output_tokens", 0)
    except (OSError, UnicodeDecodeError) as exc:
        log.debug(f"parse_transcript error  error={exc}")
        return SessionUsage(
            session_id=session_id,
            cost_usd=None,
            input_tokens=None,
            output_tokens=None,
            rate_limit_resets_at=None,
            rate_limit_used_pct=None,
            source="none",
        )

    log.debug(f"parse_transcript result  input_tokens={input_tokens}  output_tokens={output_tokens}")
    return SessionUsage(
        session_id=session_id,
        cost_usd=None,
        input_tokens=input_tokens or None,
        output_tokens=output_tokens or None,
        rate_limit_resets_at=None,
        rate_limit_used_pct=None,
        source="transcript",
    )


def get_session_usage(
    session_id: str,
    transcript_path: Optional[str] = None,
) -> SessionUsage:
    """Get usage for a session — state file first, transcript fallback."""
    log = get_logger()
    state = read_session_state(session_id)
    if state is not None:
        usage = extract_from_state(session_id, state)
        log.debug(
            f"session usage from state file  session_id={session_id}"
            f"  cost_usd={usage.cost_usd}  input_tokens={usage.input_tokens}"
            f"  output_tokens={usage.output_tokens}"
            f"  rate_limit_resets_at={usage.rate_limit_resets_at.isoformat() if usage.rate_limit_resets_at else None}"
            f"  rate_limit_used_pct={usage.rate_limit_used_pct}"
        )
        return usage

    if transcript_path:
        log.debug(f"state file missing, falling back to transcript  session_id={session_id}")
        return parse_transcript(session_id, transcript_path)

    log.debug(f"no usage source available  session_id={session_id}")
    return SessionUsage(
        session_id=session_id,
        cost_usd=None,
        input_tokens=None,
        output_tokens=None,
        rate_limit_resets_at=None,
        rate_limit_used_pct=None,
        source="none",
    )
