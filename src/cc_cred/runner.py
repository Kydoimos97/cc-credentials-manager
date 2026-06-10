import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

from cc_cred import detection, rotation
from cc_cred.store import CredStore

try:
    from claude_agent_sdk import (
        query,
        ClaudeAgentOptions,
        ProcessError,
    )
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False

RESUME_PROMPT = (
    "Continue where you left off. "
    "Your previous session was interrupted by a rate limit and a fresh credential has been loaded."
)


def _is_system_init(message: object) -> bool:
    return (
        getattr(message, "type", None) == "system"
        and getattr(message, "subtype", None) == "init"
    )


def _is_assistant(message: object) -> bool:
    return getattr(message, "type", None) == "assistant"


def _is_result(message: object) -> bool:
    return getattr(message, "type", None) == "result" or (
        hasattr(message, "is_error") and hasattr(message, "session_id")
        and not hasattr(message, "content")
    )


async def run(
    prompt: str,
    cwd: Optional[str | Path] = None,
    session_id: Optional[str] = None,
) -> int:
    """Run a Claude agent session with automatic credential rotation on rate limit.

    Returns an exit code: 0 for success, 1 for error or exhaustion.

    Force-limit env vars for testing:
      CC_CREDS_FORCE_LIMIT_N=1  (N is 1-based index of credential in store.list())
      Forces that credential to be treated as rate-limited before the SDK call.
    """
    if not _SDK_AVAILABLE:
        print("[cc-creds] claude_agent_sdk is not installed. Run: pip install claude-agent-sdk", file=sys.stderr)
        return 1

    store = CredStore()
    max_attempts = max(len(store.list()) + 1, 1)
    is_resume = session_id is not None

    for attempt in range(max_attempts):
        cred = store.get_active()

        if cred is None or not store.is_available(cred.id):
            cred = rotation.rotate(store)
            if cred is None:
                print("[cc-creds] No available credentials.", file=sys.stderr)
                return 1

        # Force-limit loop: keep rotating while the current cred is force-limited.
        # Supports CC_CREDS_FORCE_LIMIT_1, CC_CREDS_FORCE_LIMIT_2, etc.
        while True:
            all_creds = store.list()
            cred_index = next(
                (i + 1 for i, c in enumerate(all_creds) if c.id == cred.id), None
            )
            if cred_index and os.environ.get(f"CC_CREDS_FORCE_LIMIT_{cred_index}"):
                store.mark_limited(cred.id, reset_at=None)
                cred = rotation.rotate(store)
                if cred is None:
                    print("[cc-creds] All credentials exhausted (force-limit).", file=sys.stderr)
                    return 1
            else:
                break

        actual_prompt = RESUME_PROMPT if is_resume else prompt

        options = ClaudeAgentOptions(
            env={"CLAUDE_CODE_OAUTH_TOKEN": cred.token},
            cwd=cwd,
            resume=session_id if is_resume else None,
        )

        result_msg: object = None
        last_assistant_text = ""
        registered = False

        try:
            async for message in query(prompt=actual_prompt, options=options):
                if _is_system_init(message):
                    sid = getattr(message, "data", {}).get("session_id")
                    if sid and not registered:
                        session_id = sid
                        store.register_session(
                            sid,
                            cred.id,
                            str(cwd or Path.cwd()),
                            prompt[:100],
                        )
                        registered = True

                elif _is_assistant(message):
                    for block in getattr(message, "content", []):
                        text = getattr(block, "text", None)
                        if text is not None:
                            sys.stdout.write(text)
                            sys.stdout.flush()
                            last_assistant_text = text

                elif _is_result(message):
                    result_msg = message
                    sid = getattr(message, "session_id", None)
                    if sid:
                        session_id = sid

        except ProcessError as exc:
            if detection.is_rate_limited_text(str(exc)) or detection.is_rate_limited_text(last_assistant_text):
                reset_at = detection.parse_reset_time(last_assistant_text)
                store.mark_limited(cred.id, reset_at=reset_at)
                sid = getattr(result_msg, "session_id", None) if result_msg else None
                if sid:
                    store.update_session(sid, status="interrupted")
                is_resume = session_id is not None
                cred = rotation.rotate(store)
                if cred is None:
                    print("[cc-creds] All credentials exhausted after rate limit.", file=sys.stderr)
                    return 1
                continue
            print(f"[cc-creds] Process error: {exc}", file=sys.stderr)
            return getattr(exc, "exit_code", 1) or 1

        if result_msg is not None:
            usage = getattr(result_msg, "usage", None) or {}
            input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
            output_tokens = usage.get("output_tokens") if isinstance(usage, dict) else None
            cost = getattr(result_msg, "total_cost_usd", None)
            sid = getattr(result_msg, "session_id", None)
            is_error = getattr(result_msg, "is_error", False)

            if is_error:
                if detection.is_rate_limited(result_msg, last_assistant_text):
                    reset_at = detection.parse_reset_time(last_assistant_text)
                    store.mark_limited(cred.id, reset_at=reset_at)
                    if sid:
                        store.update_session(sid, cost_usd=cost, input_tokens=input_tokens,
                                             output_tokens=output_tokens, status="interrupted")
                    is_resume = True
                    cred = rotation.rotate(store)
                    if cred is None:
                        print("[cc-creds] All credentials exhausted after rate limit.", file=sys.stderr)
                        return 1
                    continue
                else:
                    if sid:
                        store.update_session(sid, cost_usd=cost, input_tokens=input_tokens,
                                             output_tokens=output_tokens, status="error")
                    return 1
            else:
                if sid:
                    store.update_session(sid, cost_usd=cost, input_tokens=input_tokens,
                                         output_tokens=output_tokens, status="success")
                return 0

    print("[cc-creds] Max rotation attempts exceeded.", file=sys.stderr)
    return 1


def run_sync(
    prompt: str,
    cwd: Optional[str | Path] = None,
    session_id: Optional[str] = None,
) -> int:
    """Synchronous wrapper around run() for use from CLI entry points."""
    return asyncio.run(run(prompt, cwd=cwd, session_id=session_id))
