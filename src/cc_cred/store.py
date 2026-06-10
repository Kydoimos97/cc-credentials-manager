from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import json
import uuid


@dataclass
class Credential:
    id: str
    token: str
    label: str
    added_at: str


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
        now = datetime.now(timezone.utc).isoformat()
        new_cred = {
            "id": cred_id,
            "token": token,
            "label": label,
            "added_at": now,
        }
        creds.append(new_cred)
        self._save_credentials(creds)

        return Credential(
            id=cred_id,
            token=token,
            label=label,
            added_at=now,
        )

    def list(self) -> list[Credential]:
        creds = self._load_credentials()
        return [
            Credential(
                id=c["id"],
                token=c["token"],
                label=c["label"],
                added_at=c["added_at"],
            )
            for c in creds
        ]

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
                return Credential(
                    id=cred["id"],
                    token=cred["token"],
                    label=cred["label"],
                    added_at=cred["added_at"],
                )
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

        active_path = self._active_path()
        active_path.write_text(id)

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
