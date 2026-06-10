"""Full lifecycle tests for runner.py using CC_CREDS_FORCE_LIMIT_N env vars
and a mocked claude_agent_sdk.query."""
import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cc_cred.store import CredStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_result(success: bool = True, session_id: str = "sess-1") -> object:
    return SimpleNamespace(
        is_error=not success,
        api_error_status=None,
        errors=None,
        session_id=session_id,
        total_cost_usd=0.01,
        usage={"input_tokens": 100, "output_tokens": 50},
        subtype="success" if success else "error_during_execution",
    )


def make_system_init(session_id: str = "sess-1") -> object:
    return SimpleNamespace(
        type="system",
        subtype="init",
        data={"session_id": session_id},
    )


async def success_query(*args, **kwargs):
    """Yields a successful session."""
    from claude_agent_sdk import SystemMessage, ResultMessage  # type: ignore
    yield make_system_init()
    yield make_result(success=True)


async def rate_limit_query(*args, **kwargs):
    """Raises ProcessError simulating a rate limit."""
    from claude_agent_sdk import ProcessError  # type: ignore
    raise ProcessError("hit your limit", exit_code=1)
    yield  # make it an async generator


@pytest.fixture
def store(tmp_path):
    with patch.object(CredStore, "STORE_DIR", new=tmp_path / ".cc-creds"):
        s = CredStore()
        yield s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not _sdk_available(),
    reason="claude_agent_sdk not installed",
)
def test_happy_path_success(store):
    c1 = store.add("sk-ant-t1", "main")
    store.set_active(c1.id)

    with patch("cc_cred.runner.CredStore", return_value=store):
        with patch("cc_cred.runner.query", new=lambda **kwargs: success_query(**kwargs)):
            from cc_cred.runner import run
            exit_code = asyncio.run(run("do a task"))

    assert exit_code == 0
    stats = store.get_stats(c1.id)
    assert stats.session_count == 1


@pytest.mark.skipif(
    not _sdk_available(),
    reason="claude_agent_sdk not installed",
)
def test_force_limit_cred1_rotates_to_cred2(store, monkeypatch):
    c1 = store.add("sk-ant-t1", "main")
    c2 = store.add("sk-ant-t2", "spare")
    store.set_active(c1.id)

    monkeypatch.setenv("CC_CREDS_FORCE_LIMIT_1", "1")

    call_count = {"n": 0}

    async def mock_query(**kwargs):
        call_count["n"] += 1
        yield make_system_init(session_id=f"sess-{call_count['n']}")
        yield make_result(success=True, session_id=f"sess-{call_count['n']}")

    with patch("cc_cred.runner.CredStore", return_value=store):
        with patch("cc_cred.runner.query", new=lambda **kwargs: mock_query(**kwargs)):
            from cc_cred.runner import run
            exit_code = asyncio.run(run("do a task"))

    assert exit_code == 0
    assert store.get_active().id == c2.id
    assert not store.is_available(c1.id)


@pytest.mark.skipif(
    not _sdk_available(),
    reason="claude_agent_sdk not installed",
)
def test_force_limit_all_returns_exit_1(store, monkeypatch):
    c1 = store.add("sk-ant-t1", "main")
    c2 = store.add("sk-ant-t2", "spare")
    store.set_active(c1.id)

    monkeypatch.setenv("CC_CREDS_FORCE_LIMIT_1", "1")
    monkeypatch.setenv("CC_CREDS_FORCE_LIMIT_2", "1")

    with patch("cc_cred.runner.CredStore", return_value=store):
        from cc_cred.runner import run
        exit_code = asyncio.run(run("do a task"))

    assert exit_code == 1


@pytest.mark.skipif(
    not _sdk_available(),
    reason="claude_agent_sdk not installed",
)
def test_force_limit_1_and_2_succeeds_on_cred3(store, monkeypatch):
    c1 = store.add("sk-ant-t1", "a")
    c2 = store.add("sk-ant-t2", "b")
    c3 = store.add("sk-ant-t3", "c")
    store.set_active(c1.id)

    monkeypatch.setenv("CC_CREDS_FORCE_LIMIT_1", "1")
    monkeypatch.setenv("CC_CREDS_FORCE_LIMIT_2", "1")

    call_count = {"n": 0}

    async def mock_query(**kwargs):
        call_count["n"] += 1
        yield make_system_init(session_id=f"sess-{call_count['n']}")
        yield make_result(success=True, session_id=f"sess-{call_count['n']}")

    with patch("cc_cred.runner.CredStore", return_value=store):
        with patch("cc_cred.runner.query", new=lambda **kwargs: mock_query(**kwargs)):
            from cc_cred.runner import run
            exit_code = asyncio.run(run("do a task"))

    assert exit_code == 0
    assert store.get_active().id == c3.id


def _sdk_available() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
        return True
    except ImportError:
        return False
