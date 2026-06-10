from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from cc_cred.store import CredStore
from cc_cred.rotation import get_next_available, rotate


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
