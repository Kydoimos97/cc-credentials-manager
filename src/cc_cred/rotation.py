from cc_cred._logging import fmt, get_logger
from cc_cred.store import CredStore, Credential
from typing import Optional
from datetime import datetime, timezone

NETWORK_FAILURE_RETRY_SECS = 300


def _fresh_check(store: CredStore, cred: Credential) -> bool:
    """Re-verify a credential via the API and update the store.

    Returns True if the fresh check says the token is available.
    On network failure, stamps last_checked so _needs_fresh_check()
    can apply a retry TTL and avoid hammering the API.
    """
    from cc_cred.verify import check_token
    log = get_logger()
    status, err = check_token(cred.token)
    log.debug(f"_fresh_check  cred={cred.id[:8]}  status={status}  err={err}")
    if status == "available":
        store.mark_available(cred.id)
        return True
    if status == "invalid":
        store.mark_admin_disabled(cred.id, error=err)
        return False
    store.mark_network_failure(cred.id, error=err or "Network check failed")
    return False


def _needs_fresh_check(store: CredStore, cred_id: str) -> bool:
    """Return True if the credential's status warrants an API re-verify."""
    status = store.get_status(cred_id)
    if status.status == "unknown":
        if status.last_checked:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(status.last_checked)).total_seconds()
            if age < NETWORK_FAILURE_RETRY_SECS:
                return False
        return True
    if status.status == "limited" and status.reset_at is None:
        if status.last_checked:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(status.last_checked)).total_seconds()
            if age < NETWORK_FAILURE_RETRY_SECS:
                return False
        return True
    return False


def get_next_available(
    store: CredStore,
    exclude_id: Optional[str] = None,
    recheck: bool = False,
) -> Optional[Credential]:
    """Return the next available credential, starting after exclude_id (round-robin).

    When recheck=True (set by rotate()), credentials with stale status
    (unknown or indefinitely-limited with no reset_at) are re-verified via
    the API before being considered. This ensures force-limit test artefacts
    and genuinely bricked credentials recover automatically once the
    underlying condition clears.
    """
    log = get_logger()
    credentials = store.list()

    log.debug(
        f"get_next_available  exclude={exclude_id[:8] if exclude_id else None}"
        + fmt([
            {"id": c.id[:8], "label": c.label, "status": store.get_status(c.id).status}
            for c in credentials
        ])
    )

    if not credentials:
        return None

    start = 0
    if exclude_id is not None:
        for i, cred in enumerate(credentials):
            if cred.id == exclude_id:
                start = (i + 1) % len(credentials)
                break

    for i in range(len(credentials)):
        idx = (start + i) % len(credentials)
        cred = credentials[idx]
        if cred.id == exclude_id:
            continue

        if recheck and _needs_fresh_check(store, cred.id):
            available = _fresh_check(store, cred)
        else:
            available = store.is_available(cred.id)

        if available:
            log.debug(f"get_next_available → {cred.id[:8]}  label={cred.label!r}")
            return cred

    log.debug("get_next_available → None (all exhausted)")
    return None


def rotate(store: CredStore) -> Optional[Credential]:
    """Advance to the next available credential and update active.key.

    Returns the new active credential, or None if all credentials are exhausted.
    """
    log = get_logger()
    current = store.get_active()
    exclude_id = current.id if current is not None else None

    log.debug(f"rotate  current={exclude_id[:8] if exclude_id else None}  label={current.label if current else None!r}")

    next_cred = get_next_available(store, exclude_id=exclude_id, recheck=True)
    if next_cred is not None:
        store.set_active(next_cred.id)
        log.debug(f"rotated  from={exclude_id[:8] if exclude_id else None}  to={next_cred.id[:8]}  label={next_cred.label!r}")
    else:
        log.debug("rotate → exhausted")

    return next_cred
