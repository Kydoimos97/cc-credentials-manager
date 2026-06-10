import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

from cc_cred import detection, rotation
from cc_cred._logging import fmt, get_logger, mask_token
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
        type(message).__name__ == "SystemMessage"
        or getattr(message, "type", None) == "system"
    ) and getattr(message, "subtype", None) == "init"


def _is_assistant(message: object) -> bool:
    return (
        type(message).__name__ == "AssistantMessage"
        or getattr(message, "type", None) == "assistant"
    )


def _is_result(message: object) -> bool:
    return (
        type(message).__name__ == "ResultMessage"
        or getattr(message, "type", None) == "result"
        or (
            hasattr(message, "is_error")
            and hasattr(message, "session_id")
            and not hasattr(message, "content")
        )
    )


def _is_rate_limit_event(message: object) -> bool:
    return type(message).__name__ == "RateLimitEvent"


def _rate_limit_status(message: object) -> tuple[str, Optional[int], Optional[float]]:
    """Extract (status, resets_at_unix, utilization_pct) from a RateLimitEvent."""
    info = getattr(message, "rate_limit_info", None)
    if info is None:
        return "unknown", None, None
    status = getattr(info, "status", "unknown")
    resets_at = getattr(info, "resets_at", None)
    utilization = getattr(info, "utilization", None)
    return status, resets_at, utilization


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

    log.debug(
        f"runner start  creds={len(all_creds)}  max_attempts={max_attempts}"
        f"  session_id={session_id}  cwd={cwd}  prompt={prompt[:80]!r}"
    )

    for attempt in range(max_attempts):
        log.debug(f"attempt {attempt}")

        cred = store.get_active()

        if cred is None or not store.is_available(cred.id):
            active_id = cred.id[:8] if cred else None
            status = store.get_status(cred.id).status if cred else "none"
            log.debug(f"active cred unavailable  id={active_id}  status={status}  → rotating")
            cred = rotation.rotate(store)
            if cred is None:
                print("[cc-creds] No available credentials.", file=sys.stderr)
                return 1

        # Force-limit loop: skip creds whose CC_CREDS_FORCE_LIMIT_N var is set.
        # Uses an in-memory skip set — does NOT write to the store, so the creds
        # remain usable in future sessions once the env vars are cleared.
        force_skipped: set[str] = set()
        while True:
            all_creds = store.list()
            cred_index = next(
                (i + 1 for i, c in enumerate(all_creds) if c.id == cred.id), None
            )
            force_var = f"CC_CREDS_FORCE_LIMIT_{cred_index}"
            if cred_index and os.environ.get(force_var):
                log.debug(f"force-limit (in-memory skip)  env_var={force_var}  cred={cred.id[:8]}")
                force_skipped.add(cred.id)
                next_cred = next(
                    (c for c in all_creds if c.id not in force_skipped and store.is_available(c.id)),
                    None,
                )
                if next_cred is None:
                    print("[cc-creds] All credentials exhausted (force-limit).", file=sys.stderr)
                    return 1
                cred = next_cred
                store.set_active(cred.id)
            else:
                break

        actual_prompt = RESUME_PROMPT if is_resume else prompt

        log.debug(
            f"SDK query  cred={cred.id[:8]}  label={cred.label!r}  token={mask_token(cred.token)}"
            f"  is_resume={is_resume}  resume_id={session_id}  cwd={cwd}"
            f"  prompt={actual_prompt[:80]!r}"
        )

        # Set at the process level too so any subprocess the agent spawns
        # (hooks, shell commands) inherits the correct token, and so it
        # shadows any ambient CLAUDE_CODE_OAUTH_TOKEN already in the shell.
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = cred.token

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
                log.debug(f"SDK ← {msg_type}/{msg_subtype}" + fmt(repr(message)))

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
                        log.debug(f"session registered  session_id={sid}  cred={cred.id[:8]}")

                elif _is_assistant(message):
                    for block in getattr(message, "content", []):
                        text = getattr(block, "text", None)
                        if text is not None:
                            sys.stdout.write(text)
                            sys.stdout.flush()
                            last_assistant_text = text

                elif _is_rate_limit_event(message):
                    rl_status, rl_resets_at, rl_utilization = _rate_limit_status(message)
                    log.debug(
                        f"RateLimitEvent  status={rl_status}"
                        f"  resets_at={rl_resets_at}  utilization={rl_utilization}"
                        + fmt(getattr(getattr(message, "rate_limit_info", None), "raw", None))
                    )
                    if rl_status != "allowed":
                        from datetime import datetime, timezone as tz
                        reset_dt = (
                            datetime.fromtimestamp(rl_resets_at, tz=tz.utc)
                            if rl_resets_at else None
                        )
                        log.debug(f"rate limit active via RateLimitEvent  rotating  reset_at={reset_dt}")
                        store.mark_limited(cred.id, reset_at=reset_dt)
                        is_resume = session_id is not None
                        cred = rotation.rotate(store)
                        if cred is None:
                            print("[cc-creds] All credentials exhausted (rate limit event).", file=sys.stderr)
                            return 1
                        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = cred.token

                elif _is_result(message):
                    result_msg = message
                    sid = getattr(message, "session_id", None)
                    if sid:
                        session_id = sid
                    log.debug(
                        f"result  session_id={sid}"
                        f"  is_error={getattr(message, 'is_error', None)}"
                        f"  subtype={getattr(message, 'subtype', None)}"
                        f"  num_turns={getattr(message, 'num_turns', None)}"
                        + fmt({
                            "api_error_status": getattr(message, "api_error_status", None),
                            "errors": getattr(message, "errors", None),
                            "cost_usd": getattr(message, "total_cost_usd", None),
                            "usage": getattr(message, "usage", None),
                        })
                    )

        except ProcessError as exc:
            log.debug(
                f"ProcessError  exit_code={getattr(exc, 'exit_code', None)}"
                f"  is_rate_limit={detection.is_rate_limited_text(str(exc))}"
                f"  error={exc!s}"
                f"  last_text={last_assistant_text[:200]!r}"
            )
            if detection.is_rate_limited_text(str(exc)) or detection.is_rate_limited_text(last_assistant_text):
                reset_at = detection.parse_reset_time(last_assistant_text)
                store.mark_limited(cred.id, reset_at=reset_at)
                log.debug(f"rate limit via ProcessError  cred={cred.id[:8]}  reset_at={reset_at.isoformat() if reset_at else None}  → rotating")
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
                    log.debug(f"rate limit via ResultMessage  cred={cred.id[:8]}  reset_at={reset_at.isoformat() if reset_at else None}  api_error_status={getattr(result_msg, 'api_error_status', None)}  → rotating")
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
                    log.debug(f"non-rate-limit error  subtype={getattr(result_msg, 'subtype', None)}  errors={getattr(result_msg, 'errors', None)}  → exit 1")
                    if sid:
                        store.update_session(sid, cost_usd=cost, input_tokens=input_tokens,
                                             output_tokens=output_tokens, status="error")
                    return 1
            else:
                log.debug(f"success  session_id={sid}  cost_usd={cost}  input_tokens={input_tokens}  output_tokens={output_tokens}")
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
