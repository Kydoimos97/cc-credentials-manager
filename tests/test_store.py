import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from cc_cred.store import CredStore, Credential, TokenStatus, CredStats


@pytest.fixture
def store(tmp_path):
    with patch.object(CredStore, "STORE_DIR", new=tmp_path / ".cc-creds"):
        s = CredStore()
        yield s


def test_add_returns_credential(store):
    cred = store.add("sk-ant-token1", "main")
    assert isinstance(cred, Credential)
    assert cred.token == "sk-ant-token1"
    assert cred.label == "main"
    assert len(cred.id) == 36


def test_add_duplicate_raises(store):
    store.add("sk-ant-token1", "main")
    with pytest.raises(ValueError):
        store.add("sk-ant-token1", "other")


def test_list_returns_all_in_order(store):
    store.add("sk-ant-t1", "a")
    store.add("sk-ant-t2", "b")
    creds = store.list()
    assert len(creds) == 2
    assert creds[0].label == "a"
    assert creds[1].label == "b"


def test_remove_deletes_credential(store):
    cred = store.add("sk-ant-t1", "a")
    store.remove(cred.id)
    assert store.list() == []


def test_remove_unknown_raises(store):
    with pytest.raises(KeyError):
        store.remove("nonexistent-id")


def test_remove_clears_active(store):
    cred = store.add("sk-ant-t1", "a")
    store.set_active(cred.id)
    store.remove(cred.id)
    assert store.get_active() is None


def test_get_active_none_when_empty(store):
    assert store.get_active() is None


def test_set_and_get_active(store):
    cred = store.add("sk-ant-t1", "a")
    store.set_active(cred.id)
    active = store.get_active()
    assert active is not None
    assert active.id == cred.id


def test_set_active_unknown_raises(store):
    with pytest.raises(KeyError):
        store.set_active("nonexistent")


def test_get_status_unknown_for_new_cred(store):
    cred = store.add("sk-ant-t1", "a")
    status = store.get_status(cred.id)
    assert status.status == "unknown"


def test_mark_limited_and_get_status(store):
    cred = store.add("sk-ant-t1", "a")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    store.mark_limited(cred.id, reset_at=future)
    status = store.get_status(cred.id)
    assert status.status == "limited"
    assert status.reset_at is not None


def test_is_available_unknown_is_true(store):
    cred = store.add("sk-ant-t1", "a")
    assert store.is_available(cred.id) is True


def test_is_available_limited_future_is_false(store):
    cred = store.add("sk-ant-t1", "a")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    store.mark_limited(cred.id, reset_at=future)
    assert store.is_available(cred.id) is False


def test_is_available_limited_past_auto_promotes(store):
    cred = store.add("sk-ant-t1", "a")
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    store.mark_limited(cred.id, reset_at=past)
    assert store.is_available(cred.id) is True
    assert store.get_status(cred.id).status == "available"


def test_is_available_admin_disabled_is_false(store):
    cred = store.add("sk-ant-t1", "a")
    status_dict = {cred.id: {"status": "admin_disabled", "reset_at": None, "last_checked": None, "last_error": None}}
    with open(store._status_path(), "w") as f:
        json.dump(status_dict, f)
    assert store.is_available(cred.id) is False


def test_mark_available(store):
    cred = store.add("sk-ant-t1", "a")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    store.mark_limited(cred.id, reset_at=future)
    store.mark_available(cred.id)
    assert store.get_status(cred.id).status == "available"


def test_register_and_update_session(store):
    cred = store.add("sk-ant-t1", "a")
    store.register_session("sess-1", cred.id, "/tmp", "do something")

    sessions_path = store._sessions_path()
    assert sessions_path.exists()
    lines = sessions_path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["session_id"] == "sess-1"
    assert record["status"] == "running"

    store.update_session("sess-1", cost_usd=0.05, input_tokens=100, output_tokens=50, status="success")
    lines = sessions_path.read_text().strip().splitlines()
    record = json.loads(lines[0])
    assert record["status"] == "success"
    assert record["cost_usd"] == 0.05


def test_update_session_not_found_is_noop(store):
    store.register_session("sess-1", "cred-id", "/tmp", "prompt")
    store.update_session("nonexistent", status="success")
    lines = store._sessions_path().read_text().strip().splitlines()
    assert json.loads(lines[0])["status"] == "running"


def test_get_stats_aggregates(store):
    cred = store.add("sk-ant-t1", "a")
    store.register_session("s1", cred.id, "/", "p")
    store.update_session("s1", cost_usd=0.01, input_tokens=100, output_tokens=50, status="success")
    store.register_session("s2", cred.id, "/", "p")
    store.update_session("s2", cost_usd=0.02, input_tokens=200, output_tokens=100, status="success")

    stats = store.get_stats(cred.id)
    assert stats.session_count == 2
    assert abs(stats.total_cost_usd - 0.03) < 1e-9
    assert stats.total_input_tokens == 300
    assert stats.total_output_tokens == 150


def test_get_stats_empty(store):
    cred = store.add("sk-ant-t1", "a")
    stats = store.get_stats(cred.id)
    assert stats.session_count == 0
    assert stats.total_cost_usd == 0.0
