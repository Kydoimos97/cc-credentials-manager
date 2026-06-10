from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import json
import uuid

TOKEN_LIFETIME_DAYS = 365


def _log():
    from cc_cred._logging import get_logger
    return get_logger()


@dataclass
class Credential:
    id: str
    token: str
    label: str
    added_at: str
    expires_at: Optional[str] = None  # ISO datetime, added_at + TOKEN_LIFETIME_DAYS


@dataclass
class TokenStatus:
    status: str
    reset_at: Optional[str] = None
    last_checked: Optional[str] = None
    last_error: Optional[str] = None


@dataclass
class CredStats:
    credential_id: str
    session_count: int
    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int


def _cred_from_dict(c: dict) -> "Credential":
    """Hydrate a Credential from a stored dict, tolerating missing expires_at."""
    return Credential(
        id=c["id"],
        token=c["token"],
        label=c["label"],
        added_at=c["added_at"],
        expires_at=c.get("expires_at"),
    )


class CredStore:
    STORE_DIR: Path = Path.home() / ".cc-creds"

    def __init__(self) -> None:
        self.STORE_DIR.mkdir(parents=True, exist_ok=True)

    def _credentials_path(self) -> Path:
        return self.STORE_DIR / "credentials.json"

    def _status_path(self) -> Path:
        return self.STORE_DIR / "cred-status.json"

    def _active_path(self) -> Path:
        return self.STORE_DIR / "active.key"

    def _sessions_path(self) -> Path:
        return self.STORE_DIR / "sessions.jsonl"

    def _load_credentials(self) -> list[dict]:
        path = self._credentials_path()
        if not path.exists():
            return []
        with open(path, "r") as f:
            return json.load(f)

    def _save_credentials(self, creds: list[dict]) -> None:
        path = self._credentials_path()
        with open(path, "w") as f:
            json.dump(creds, f, indent=2)

    def _load_status(self) -> dict:
        path = self._status_path()
        if not path.exists():
            return {}
        with open(path, "r") as f:
            return json.load(f)

    def _save_status(self, status: dict) -> None:
        path = self._status_path()
        with open(path, "w") as f:
            json.dump(status, f, indent=2)

    def add(self, token: str, label: str = "") -> Credential:
        creds = self._load_credentials()
        for cred in creds:
            if cred["token"] == token:
                raise ValueError("Token already registered")

        cred_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()
        expires_str = (now + timedelta(days=TOKEN_LIFETIME_DAYS)).isoformat()

        new_cred = {
            "id": cred_id,
            "token": token,
            "label": label,
            "added_at": now_str,
            "expires_at": expires_str,
        }
        creds.append(new_cred)
        self._save_credentials(creds)

        return Credential(
            id=cred_id,
            token=token,
            label=label,
            added_at=now_str,
            expires_at=expires_str,
        )

    def list(self) -> list[Credential]:
        creds = self._load_credentials()
        return [_cred_from_dict(c) for c in creds]

    def remove(self, id: str) -> None:
        creds = self._load_credentials()
        found = False
        for i, cred in enumerate(creds):
            if cred["id"] == id:
                del creds[i]
                found = True
                break

        if not found:
            raise KeyError(f"Credential {id} not found")

        self._save_credentials(creds)

        status = self._load_status()
        if id in status:
            del status[id]
            self._save_status(status)

        active_path = self._active_path()
        if active_path.exists():
            active_id = active_path.read_text().strip()
            if active_id == id:
                active_path.unlink()

    def get(self, id: str) -> Optional[Credential]:
        creds = self._load_credentials()
        for cred in creds:
            if cred["id"] == id:
                return _cred_from_dict(cred)
        return None

    def get_active(self) -> Optional[Credential]:
        active_path = self._active_path()
        if not active_path.exists():
            return None

        active_id = active_path.read_text().strip()
        return self.get(active_id)

    def set_active(self, id: str) -> None:
        cred = self.get(id)
        if cred is None:
            raise KeyError(f"Credential {id} not found")

        _log().debug("set_active", extra={"id": id[:8], "label": cred.label})
        active_path = self._active_path()
        active_path.write_text(id)
        self.sync_to_settings(cred)

    def sync_to_settings(self, cred: "Credential") -> None:
        """Write cred.token to ~/.claude/settings.json env.CLAUDE_CODE_OAUTH_TOKEN.

        This ensures every claude invocation (interactive or SDK) uses the
        currently active credential without any shell-level env var wrangling.
        Silently does nothing if settings.json does not exist.
        """
        import json as _json
        from cc_cred._logging import mask_token
        settings_path = Path.home() / ".claude" / "settings.json"

        _log().debug("sync_to_settings", extra={
            "credential_id": cred.id[:8],
            "label": cred.label,
            "token": mask_token(cred.token),
            "settings_path": str(settings_path),
            "settings_exists": settings_path.exists(),
        })

        if not settings_path.exists():
            return

        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = _json.load(f)

            settings.setdefault("env", {})["CLAUDE_CODE_OAUTH_TOKEN"] = cred.token

            tmp_path = settings_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                _json.dump(settings, f, indent=2)
            tmp_path.replace(settings_path)
            _log().debug("sync_to_settings complete", extra={"settings_path": str(settings_path)})
        except (OSError, ValueError) as exc:
            _log().debug("sync_to_settings failed", extra={"error": str(exc)})

    def get_status(self, id: str) -> TokenStatus:
        status_dict = self._load_status()
        if id not in status_dict:
            return TokenStatus(status="unknown")

        s = status_dict[id]
        return TokenStatus(
            status=s.get("status", "unknown"),
            reset_at=s.get("reset_at"),
            last_checked=s.get("last_checked"),
            last_error=s.get("last_error"),
        )

    def is_available(self, id: str) -> bool:
        status = self.get_status(id)

        if status.status == "admin_disabled":
            return False

        if status.status == "available" or status.status == "unknown":
            return True

        if status.status == "limited":
            if status.reset_at is None:
                return False

            reset_dt = datetime.fromisoformat(status.reset_at)
            now = datetime.now(timezone.utc)

            if reset_dt <= now:
                self.mark_available(id)
                return True

            return False

        return True

    def mark_limited(self, id: str, reset_at: Optional[datetime] = None) -> None:
        _log().debug("mark_limited", extra={
            "id": id[:8],
            "reset_at": reset_at.isoformat() if reset_at else None,
        })
        status_dict = self._load_status()
        reset_str = None
        if reset_at is not None:
            reset_str = reset_at.isoformat()

        status_dict[id] = {
            "status": "limited",
            "reset_at": reset_str,
            "last_checked": datetime.now(timezone.utc).isoformat(),
            "last_error": None,
        }
        self._save_status(status_dict)

    def mark_available(self, id: str) -> None:
        _log().debug("mark_available", extra={"id": id[:8]})
        status_dict = self._load_status()
        status_dict[id] = {
            "status": "available",
            "reset_at": None,
            "last_checked": datetime.now(timezone.utc).isoformat(),
            "last_error": None,
        }
        self._save_status(status_dict)

    def register_session(
        self,
        session_id: str,
        credential_id: str,
        cwd: str,
        prompt_summary: str,
    ) -> None:
        sessions_path = self._sessions_path()
        summary = prompt_summary[:100]

        record = {
            "session_id": session_id,
            "credential_id": credential_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "cwd": cwd,
            "prompt_summary": summary,
            "cost_usd": None,
            "input_tokens": None,
            "output_tokens": None,
            "status": "running",
        }

        with open(sessions_path, "a") as f:
            f.write(json.dumps(record) + "\n")

    def session_exists(self, session_id: str) -> bool:
        """Return True if session_id already has a record in sessions.jsonl."""
        path = self._sessions_path()
        if not path.exists():
            return False
        with open(path, "r") as f:
            for line in f:
                try:
                    if json.loads(line).get("session_id") == session_id:
                        return True
                except json.JSONDecodeError:
                    continue
        return False

    def update_session(
        self,
        session_id: str,
        *,
        cost_usd: Optional[float] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        status: str = "success",
    ) -> None:
        sessions_path = self._sessions_path()
        if not sessions_path.exists():
            return

        with open(sessions_path, "r") as f:
            lines = f.readlines()

        matching_idx = None
        for i in range(len(lines) - 1, -1, -1):
            record = json.loads(lines[i])
            if record.get("session_id") == session_id:
                matching_idx = i
                break

        if matching_idx is None:
            return

        record = json.loads(lines[matching_idx])
        if cost_usd is not None:
            record["cost_usd"] = cost_usd
        if input_tokens is not None:
            record["input_tokens"] = input_tokens
        if output_tokens is not None:
            record["output_tokens"] = output_tokens
        record["status"] = status

        lines[matching_idx] = json.dumps(record) + "\n"

        with open(sessions_path, "w") as f:
            f.writelines(lines)

    def get_stats(self, credential_id: str) -> CredStats:
        sessions_path = self._sessions_path()
        if not sessions_path.exists():
            return CredStats(
                credential_id=credential_id,
                session_count=0,
                total_cost_usd=0.0,
                total_input_tokens=0,
                total_output_tokens=0,
            )

        session_count = 0
        total_cost = 0.0
        total_input = 0
        total_output = 0

        with open(sessions_path, "r") as f:
            for line in f:
                record = json.loads(line)
                if record.get("credential_id") == credential_id:
                    session_count += 1
                    cost = record.get("cost_usd")
                    if cost is not None:
                        total_cost += cost
                    inp = record.get("input_tokens")
                    if inp is not None:
                        total_input += inp
                    out = record.get("output_tokens")
                    if out is not None:
                        total_output += out

        return CredStats(
            credential_id=credential_id,
            session_count=session_count,
            total_cost_usd=total_cost,
            total_input_tokens=total_input,
            total_output_tokens=total_output,
        )
