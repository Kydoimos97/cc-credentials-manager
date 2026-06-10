import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

from cc_cred import detection, rotation
from cc_cred._logging import get_logger, mask_token
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

    log = get_logger()
    store = CredStore()
    all_creds = store.list()
    max_attempts = max(len(all_creds) + 1, 1)
    is_resume = session_id is not None

    log.debug("Runner starting", extra={
        "prompt_preview": prompt[:120],
        "cwd": str(cwd),
        "session_id": session_id,
        "credential_count": len(all_creds),
        "max_attempts": max_attempts,
    })

    for attempt in range(max_attempts):
        log.debug("Attempt starting", extra={"attempt": attempt})

        cred = store.get_active()

        if cred is None or not store.is_available(cred.id):
            log.debug("Active credential unavailable, rotating", extra={
                "active_id": cred.id[:8] if cred else None,
                "status": store.get_status(cred.id).status if cred else "none",
            })
            cred = rotation.rotate(store)
            if cred is None:
                print("[cc-creds] No available credentials.", file=sys.stderr)
                return 1

        # Force-limit loop: keep rotating while the current cred is force-limited.
        while True:
            all_creds = store.list()
            cred_index = next(
                (i + 1 for i, c in enumerate(all_creds) if c.id == cred.id), None
            )
            force_var = f"CC_CREDS_FORCE_LIMIT_{cred_index}"
            if cred_index and os.environ.get(force_var):
                log.debug("Force-limit env var active, marking limited and rotating", extra={
                    "env_var": force_var,
                    "cred_id": cred.id[:8],
                    "label": cred.label,
                })
                store.mark_limited(cred.id, reset_at=None)
                cred = rotation.rotate(store)
                if cred is None:
                    print("[cc-creds] All credentials exhausted (force-limit).", file=sys.stderr)
                    return 1
            else:
                break

        actual_prompt = RESUME_PROMPT if is_resume else prompt

        log.debug("Invoking SDK query", extra={
            "credential_id": cred.id[:8],
            "credential_label": cred.label,
            "token": mask_token(cred.token),
            "is_resume": is_resume,
            "resume_session_id": session_id,
            "prompt_preview": actual_prompt[:120],
            "cwd": str(cwd),
        })

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
                msg_type = getattr(message, "type", type(message).__name__)
                msg_subtype = getattr(message, "subtype", None)
                log.debug("SDK message received", extra={
                    "type": msg_type,
                    "subtype": msg_subtype,
                    "raw": repr(message)[:500],
                })

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
                        log.debug("Session registered", extra={
                            "session_id": sid,
                            "credential_id": cred.id[:8],
                        })

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
                    log.debug("Result message received", extra={
                        "session_id": sid,
                        "is_error": getattr(message, "is_error", None),
                        "subtype": getattr(message, "subtype", None),
                        "api_error_status": getattr(message, "api_error_status", None),
                        "errors": getattr(message, "errors", None),
                        "total_cost_usd": getattr(message, "total_cost_usd", None),
                        "usage": getattr(message, "usage", None),
                        "num_turns": getattr(message, "num_turns", None),
                    })

        except ProcessError as exc:
            log.debug("ProcessError caught", extra={
                "error": str(exc),
                "exit_code": getattr(exc, "exit_code", None),
                "is_rate_limit": detection.is_rate_limited_text(str(exc)),
                "last_assistant_text_preview": last_assistant_text[:200],
            })
            if detection.is_rate_limited_text(str(exc)) or detection.is_rate_limited_text(last_assistant_text):
                reset_at = detection.parse_reset_time(last_assistant_text)
                store.mark_limited(cred.id, reset_at=reset_at)
                log.debug("Rate limit detected via ProcessError, rotating", extra={
                    "cred_id": cred.id[:8],
                    "reset_at": reset_at.isoformat() if reset_at else None,
                })
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
                    log.debug("Rate limit detected via ResultMessage, rotating", extra={
                        "cred_id": cred.id[:8],
                        "reset_at": reset_at.isoformat() if reset_at else None,
                        "api_error_status": getattr(result_msg, "api_error_status", None),
                    })
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
                    log.debug("Non-rate-limit error result, returning exit 1", extra={
                        "subtype": getattr(result_msg, "subtype", None),
                        "errors": getattr(result_msg, "errors", None),
                    })
                    if sid:
                        store.update_session(sid, cost_usd=cost, input_tokens=input_tokens,
                                             output_tokens=output_tokens, status="error")
                    return 1
            else:
                log.debug("Successful result", extra={
                    "session_id": sid,
                    "cost_usd": cost,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                })
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
