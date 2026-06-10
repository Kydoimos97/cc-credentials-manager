from cc_cred._logging import get_logger
from cc_cred.store import CredStore, Credential
from typing import Optional


def get_next_available(
    store: CredStore,
    exclude_id: Optional[str] = None,
) -> Optional[Credential]:
    """Return the next available credential, starting after exclude_id (round-robin).

    Iterates credentials in insertion order beginning one position after exclude_id.
    Wraps around to the start. Returns the first credential where
    store.is_available() is True and id != exclude_id.
    Returns None if no available credential exists.
    """
    log = get_logger()
    credentials = store.list()

    candidates = [
        f"{c.id[:8]}({c.label or '-'})={'✓' if store.is_available(c.id) else '✗'}"
        for c in credentials
    ]
    log.debug(f"get_next_available  exclude={exclude_id[:8] if exclude_id else None}  candidates=[{', '.join(candidates)}]")

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
        if store.is_available(cred.id):
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

    next_cred = get_next_available(store, exclude_id=exclude_id)
    if next_cred is not None:
        store.set_active(next_cred.id)
        log.debug(f"rotated  from={exclude_id[:8] if exclude_id else None}  to={next_cred.id[:8]}  label={next_cred.label!r}")
    else:
        log.debug("rotate → exhausted")

    return next_cred
