import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

from cc_cred import detection, rotation
from cc_cred.store import CredStore

RESUME_PROMPT = (
    "Continue where you left off. "
    "Your previous session was interrupted by a rate limit and a fresh credential has been loaded."
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
    from claude_agent_sdk import (
        query,
        ClaudeAgentOptions,
        AssistantMessage,
        ResultMessage,
        SystemMessage,
        ProcessError,
    )
    from claude_agent_sdk import TextBlock

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

        # Force-limit check for testing
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

        actual_prompt = RESUME_PROMPT if is_resume else prompt

        options = ClaudeAgentOptions(
            env={"CLAUDE_CODE_OAUTH_TOKEN": cred.token},
            cwd=cwd,
            resume=session_id if is_resume else None,
        )

        result_msg: Optional[ResultMessage] = None
        last_assistant_text = ""
        registered = False

        try:
            async for message in query(prompt=actual_prompt, options=options):
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    sid = message.data.get("session_id")
                    if sid and not registered:
                        session_id = sid
                        store.register_session(
                            sid,
                            cred.id,
                            str(cwd or Path.cwd()),
                            prompt[:100],
                        )
                        registered = True

                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            sys.stdout.write(block.text)
                            sys.stdout.flush()
                            last_assistant_text = block.text

                elif isinstance(message, ResultMessage):
                    result_msg = message
                    if result_msg.session_id:
                        session_id = result_msg.session_id

        except ProcessError as exc:
            if detection.is_rate_limited_text(str(exc)) or detection.is_rate_limited_text(last_assistant_text):
                reset_at = detection.parse_reset_time(last_assistant_text)
                store.mark_limited(cred.id, reset_at=reset_at)
                if result_msg and result_msg.session_id:
                    store.update_session(result_msg.session_id, status="interrupted")
                is_resume = session_id is not None
                cred = rotation.rotate(store)
                if cred is None:
                    print("[cc-creds] All credentials exhausted after rate limit.", file=sys.stderr)
                    return 1
                continue
            print(f"[cc-creds] Process error: {exc}", file=sys.stderr)
            return getattr(exc, "exit_code", 1) or 1

        if result_msg is not None:
            input_tokens = None
            output_tokens = None
            if result_msg.usage:
                input_tokens = result_msg.usage.get("input_tokens")
                output_tokens = result_msg.usage.get("output_tokens")

            if result_msg.is_error:
                if detection.is_rate_limited(result_msg, last_assistant_text):
                    reset_at = detection.parse_reset_time(last_assistant_text)
                    store.mark_limited(cred.id, reset_at=reset_at)
                    store.update_session(
                        result_msg.session_id,
                        cost_usd=result_msg.total_cost_usd,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        status="interrupted",
                    )
                    is_resume = True
                    cred = rotation.rotate(store)
                    if cred is None:
                        print("[cc-creds] All credentials exhausted after rate limit.", file=sys.stderr)
                        return 1
                    continue
                else:
                    store.update_session(
                        result_msg.session_id,
                        cost_usd=result_msg.total_cost_usd,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        status="error",
                    )
                    return 1
            else:
                store.update_session(
                    result_msg.session_id,
                    cost_usd=result_msg.total_cost_usd,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    status="success",
                )
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
