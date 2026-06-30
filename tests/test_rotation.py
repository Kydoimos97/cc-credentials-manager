from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import json

import pytest

from cc_cred.store import CredStore
from cc_cred.rotation import get_next_available, rotate, _needs_fresh_check


@pytest.fixture(autouse=True)
def no_fresh_check():
    """Prevent _fresh_check from making real HTTP calls in unit tests.
    rotate() passes recheck=True which would call check_token on unknown-status
    credentials — fake tokens would always return invalid and break the tests."""
    with patch("cc_cred.rotation._fresh_check", return_value=True):
        yield


@pytest.fixture
def store(tmp_path):
    with patch.object(CredStore, "STORE_DIR", new=tmp_path / ".cc-creds"):
        s = CredStore()
        yield s


def test_get_next_available_empty(store):
    assert get_next_available(store) is None


def test_get_next_available_single(store):
    cred = store.add("sk-ant-t1", "a")
    result = get_next_available(store)
    assert result is not None
    assert result.id == cred.id


def test_get_next_available_skips_exclude(store):
    c1 = store.add("sk-ant-t1", "a")
    c2 = store.add("sk-ant-t2", "b")
    result = get_next_available(store, exclude_id=c1.id)
    assert result is not None
    assert result.id == c2.id


def test_get_next_available_wraps_around(store):
    c1 = store.add("sk-ant-t1", "a")
    store.add("sk-ant-t2", "b")
    c3 = store.add("sk-ant-t3", "c")
    # Exclude c3 (last): should wrap to c1
    result = get_next_available(store, exclude_id=c3.id)
    assert result is not None
    assert result.id == c1.id


def test_get_next_available_skips_limited(store):
    c1 = store.add("sk-ant-t1", "a")
    c2 = store.add("sk-ant-t2", "b")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    store.mark_limited(c1.id, reset_at=future)
    result = get_next_available(store)
    assert result is not None
    assert result.id == c2.id


def test_get_next_available_none_when_all_limited(store):
    c1 = store.add("sk-ant-t1", "a")
    c2 = store.add("sk-ant-t2", "b")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    store.mark_limited(c1.id, reset_at=future)
    store.mark_limited(c2.id, reset_at=future)
    assert get_next_available(store) is None


def test_get_next_available_promotes_expired(store):
    c1 = store.add("sk-ant-t1", "a")
    c2 = store.add("sk-ant-t2", "b")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    store.mark_limited(c1.id, reset_at=future)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    store.mark_limited(c2.id, reset_at=past)
    # c2 has expired limit — should be auto-promoted and returned
    result = get_next_available(store, exclude_id=c1.id)
    assert result is not None
    assert result.id == c2.id


def test_rotate_sets_active(store):
    c1 = store.add("sk-ant-t1", "a")
    c2 = store.add("sk-ant-t2", "b")
    store.set_active(c1.id)
    next_cred = rotate(store)
    assert next_cred is not None
    assert next_cred.id == c2.id
    assert store.get_active().id == c2.id


def test_rotate_returns_none_when_exhausted(store):
    c1 = store.add("sk-ant-t1", "a")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    store.mark_limited(c1.id, reset_at=future)
    store.set_active(c1.id)
    result = rotate(store)
    assert result is None


def test_rotate_with_no_active(store):
    c1 = store.add("sk-ant-t1", "a")
    result = rotate(store)
    assert result is not None
    assert result.id == c1.id


def test_needs_fresh_check_limited_with_reset_at_stale(store):
    """Test that _needs_fresh_check returns True for limited with future reset_at after retry TTL.

    This was the broken case: _needs_fresh_check previously had 'and status.reset_at is None',
    which meant it never re-verified a limited credential with a future reset_at timestamp.
    """
    c1 = store.add("sk-ant-t1", "a")
    future = datetime.now(timezone.utc) + timedelta(hours=8)
    store.mark_limited(c1.id, reset_at=future)

    # Backdate last_checked to 1 hour ago (> NETWORK_FAILURE_RETRY_SECS)
    status_path = store._status_path()
    status_dict = json.loads(status_path.read_text())
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    status_dict[c1.id]["last_checked"] = one_hour_ago
    status_path.write_text(json.dumps(status_dict))

    # Should return True because age > NETWORK_FAILURE_RETRY_SECS
    assert _needs_fresh_check(store, c1.id) is True


def test_rotate_recovers_limited_with_future_reset_at(store):
    """Test that rotate can recover a limited credential with future reset_at via live check.

    Both credentials are limited with future reset_at. When rotate calls get_next_available
    with recheck=True, it discovers via _fresh_check that c2 is actually available.
    """
    c1 = store.add("sk-ant-t1", "a")
    c2 = store.add("sk-ant-t2", "b")
    store.set_active(c1.id)

    future = datetime.now(timezone.utc) + timedelta(hours=8)
    store.mark_limited(c1.id, reset_at=future)
    store.mark_limited(c2.id, reset_at=future)

    # Backdate c2's last_checked to 1 hour ago so it becomes a candidate for fresh check
    status_path = store._status_path()
    status_dict = json.loads(status_path.read_text())
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    status_dict[c2.id]["last_checked"] = one_hour_ago
    status_path.write_text(json.dumps(status_dict))

    # Patch _fresh_check to return True for c2, simulating successful live recovery
    with patch("cc_cred.rotation._fresh_check", return_value=True):
        result = rotate(store)

    assert result is not None
    assert result.id == c2.id
    assert store.get_active().id == c2.id
