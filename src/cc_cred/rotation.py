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
    credentials = store.list()
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
            return cred

    return None


def rotate(store: CredStore) -> Optional[Credential]:
    """Advance to the next available credential and update active.key.

    Returns the new active credential, or None if all credentials are exhausted.
    """
    current = store.get_active()
    exclude_id = current.id if current is not None else None

    next_cred = get_next_available(store, exclude_id=exclude_id)
    if next_cred is not None:
        store.set_active(next_cred.id)

    return next_cred
