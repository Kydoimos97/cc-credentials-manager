from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import json
import uuid
from filelock import FileLock

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


@dataclass
class DailyUsage:
    date: str
    sessions: int
    cost_usd: float
    input_tokens: int
    output_tokens: int


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

    def _credentials_lock(self) -> FileLock:
        return FileLock(str(self._credentials_path()) + ".lock")

    def _status_lock(self) -> FileLock:
        return FileLock(str(self._status_path()) + ".lock")

    def _sessions_lock(self) -> FileLock:
        return FileLock(str(self._sessions_path()) + ".lock")

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
        with self._credentials_lock():
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
        removed_cred = None
        found = False
        for i, cred in enumerate(creds):
            if cred["id"] == id:
                removed_cred = _cred_from_dict(cred)
                del creds[i]
                found = True
                break

        if not found:
            raise KeyError(f"Credential {id} not found")

        with self._credentials_lock():
            self._save_credentials(creds)

        with self._status_lock():
            status = self._load_status()
            if id in status:
                del status[id]
                self._save_status(status)

        active_path = self._active_path()
        was_active = False
        if active_path.exists():
            active_id = active_path.read_text().strip()
            if active_id == id:
                active_path.unlink()
                was_active = True

        if was_active and removed_cred is not None:
            self._scrub_token_from_layers(removed_cred.token)

    def get(self, id: str) -> Optional[Credential]:
        creds = self._load_credentials()
        for cred in creds:
            if cred["id"] == id:
                return _cred_from_dict(cred)
        return None

    def get_by_token(self, token: str) -> Optional[Credential]:
        """Return the credential whose token matches, or None."""
        for cred in self._load_credentials():
            if cred.get("token") == token:
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

        _log().debug(f"set_active  id={id[:8]}  label={cred.label!r}")
        active_path = self._active_path()
        active_path.write_text(id)
        self._scrub_settings_json_token()
        self.sync_to_settings(cred)

    def _scrub_settings_json_token(self) -> None:
        """Remove any CLAUDE_CODE_OAUTH_TOKEN from settings.json env block.

        One-time migration: older versions of this tool wrote the token there,
        which breaks interactive Claude Code sessions. Called on every set_active
        so existing installs are cleaned up automatically.
        """
        import json as _json
        settings_path = Path.home() / ".claude" / "settings.json"
        if not settings_path.exists():
            return
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = _json.load(f)
            env_block = settings.get("env", {})
            if "CLAUDE_CODE_OAUTH_TOKEN" not in env_block:
                return
            del env_block["CLAUDE_CODE_OAUTH_TOKEN"]
            if not env_block:
                settings.pop("env", None)
            else:
                settings["env"] = env_block
            tmp_path = settings_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                _json.dump(settings, f, indent=2)
            tmp_path.replace(settings_path)
            _log().debug(f"_scrub_settings_json_token: removed legacy token  path={settings_path}")
        except (OSError, ValueError) as exc:
            _log().debug(f"_scrub_settings_json_token: failed  error={exc}")

    def sync_to_settings(self, cred: "Credential") -> None:
        """Push the active token to os.environ and platform persistence.

        1. os.environ — immediate effect for the current process.
        2. Platform persistence (for new terminals):
             Windows: HKCU\\Environment registry key (winreg)
             Other:   ~/.cc-creds/env file (source from .bashrc/.zshrc)

        Does NOT touch settings.json — writing the token there breaks
        interactive Claude Code sessions which rely on the real OAuth session.
        """
        import os as _os
        from cc_cred._logging import mask_token

        _log().debug(f"sync_to_settings  cred={cred.id[:8]}  label={cred.label!r}  token={mask_token(cred.token)}")

        # 1. Current process env
        _os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = cred.token

        # 2. Platform persistence
        if _os.name == "nt":
            try:
                import winreg
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
                ) as key:
                    winreg.SetValueEx(key, "CLAUDE_CODE_OAUTH_TOKEN", 0, winreg.REG_SZ, cred.token)
                _log().debug("sync_to_settings: wrote to HKCU\\Environment")
            except Exception as exc:
                _log().debug(f"sync_to_settings: winreg write failed  error={exc}")
        else:
            try:
                env_file = self.STORE_DIR / "env"
                env_file.write_text(f'export CLAUDE_CODE_OAUTH_TOKEN="{cred.token}"\n', encoding="utf-8")
                _log().debug(f"sync_to_settings: wrote env file  path={env_file}")
            except OSError as exc:
                _log().debug(f"sync_to_settings: env file write failed  error={exc}")

    def deactivate(self) -> None:
        """Clear the active credential and remove the token from all persistence layers.

        Removes CLAUDE_CODE_OAUTH_TOKEN from:
        - os.environ (current process)
        - HKCU\\Environment registry (Windows) or ~/.cc-creds/env file (other)
        - settings.json env block (legacy cleanup)
        - active.key (so get_active() returns None)
        """
        import os as _os
        import json as _json

        _log().debug("deactivate")

        # Clear active.key
        active_path = self._active_path()
        if active_path.exists():
            active_path.unlink()

        # Current process env
        _os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

        # Platform persistence
        if _os.name == "nt":
            try:
                import winreg
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
                ) as key:
                    try:
                        winreg.DeleteValue(key, "CLAUDE_CODE_OAUTH_TOKEN")
                        _log().debug("deactivate: cleared HKCU\\Environment")
                    except FileNotFoundError:
                        pass
            except Exception as exc:
                _log().debug(f"deactivate: winreg clear failed  error={exc}")
        else:
            try:
                env_file = self.STORE_DIR / "env"
                if env_file.exists():
                    env_file.unlink()
                    _log().debug(f"deactivate: removed env file  path={env_file}")
            except OSError as exc:
                _log().debug(f"deactivate: env file clear failed  error={exc}")

        # Legacy settings.json cleanup
        self._scrub_settings_json_token()

    def _scrub_token_from_layers(self, token: str) -> None:
        """Clear the active token from os.environ and any legacy persistence layers.

        Also cleans up any CLAUDE_CODE_OAUTH_TOKEN entry in settings.json left
        by older versions of this tool — those writes break interactive sessions
        and should never have been there.
        """
        import os as _os
        import json as _json
        from cc_cred._logging import mask_token

        _log().debug(f"_scrub_token_from_layers  token={mask_token(token)}")

        # Current process env
        if _os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == token:
            del _os.environ["CLAUDE_CODE_OAUTH_TOKEN"]

        # Clean up any token written to settings.json by older versions of this tool
        settings_path = Path.home() / ".claude" / "settings.json"
        if not settings_path.exists():
            return
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = _json.load(f)
            env_block = settings.get("env", {})
            if "CLAUDE_CODE_OAUTH_TOKEN" in env_block:
                del env_block["CLAUDE_CODE_OAUTH_TOKEN"]
                if not env_block:
                    settings.pop("env", None)
                else:
                    settings["env"] = env_block
                tmp_path = settings_path.with_suffix(".tmp")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    _json.dump(settings, f, indent=2)
                tmp_path.replace(settings_path)
                _log().debug("_scrub_token_from_layers: removed legacy token from settings.json")
        except (OSError, ValueError) as exc:
            _log().debug(f"_scrub_token_from_layers: settings.json scrub failed  error={exc}")

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

        return False

    def mark_limited(self, id: str, reset_at: Optional[datetime] = None) -> None:
        _log().debug(f"mark_limited  id={id[:8]}  reset_at={reset_at.isoformat() if reset_at else None}")
        with self._status_lock():
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
        _log().debug(f"mark_available  id={id[:8]}")
        with self._status_lock():
            status_dict = self._load_status()
            status_dict[id] = {
                "status": "available",
                "reset_at": None,
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "last_error": None,
            }
            self._save_status(status_dict)

    def mark_admin_disabled(self, id: str, error: Optional[str] = None) -> None:
        _log().debug(f"mark_admin_disabled  id={id[:8]}  error={error}")
        with self._status_lock():
            status_dict = self._load_status()
            status_dict[id] = {
                "status": "admin_disabled",
                "reset_at": None,
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "last_error": error,
            }
            self._save_status(status_dict)

    def mark_network_failure(self, id: str, error: str) -> None:
        """Stamp last_checked on a network check failure without changing status.
        Used by rotation._fresh_check() so _needs_fresh_check() can gate on recency."""
        _log().debug(f"mark_network_failure  id={id[:8]}  error={error}")
        with self._status_lock():
            status_dict = self._load_status()
            current = status_dict.get(id, {})
            status_dict[id] = {
                "status": current.get("status", "unknown"),
                "reset_at": current.get("reset_at"),
                "last_checked": datetime.now(timezone.utc).isoformat(),
                "last_error": error,
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

        with self._sessions_lock():
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

        with self._sessions_lock():
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

    def get_daily_usage(self, credential_id: str, days: int = 30) -> list["DailyUsage"]:
        """Return per-day usage aggregates for the last `days` calendar days.

        Days with no sessions are included as zero-value entries so the
        caller gets a consistent-length sequence suitable for a sparkline chart.
        """
        from datetime import date, timedelta

        sessions_path = self._sessions_path()
        today = date.today()
        # Build a dict of date -> DailyUsage for the window
        window: dict[str, DailyUsage] = {}
        for i in range(days):
            d = (today - timedelta(days=days - 1 - i)).isoformat()
            window[d] = DailyUsage(
                date=d,
                sessions=0,
                cost_usd=0.0,
                input_tokens=0,
                output_tokens=0,
            )

        if not sessions_path.exists():
            return list(window.values())

        with open(sessions_path, "r") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("credential_id") != credential_id:
                    continue
                started = record.get("started_at", "")
                if not started:
                    continue
                try:
                    day_str = started[:10]  # "YYYY-MM-DD"
                except Exception:
                    continue
                if day_str not in window:
                    continue
                entry = window[day_str]
                entry.sessions += 1
                cost = record.get("cost_usd")
                if cost is not None:
                    entry.cost_usd += cost
                inp = record.get("input_tokens")
                if inp is not None:
                    entry.input_tokens += inp
                out = record.get("output_tokens")
                if out is not None:
                    entry.output_tokens += out

        return list(window.values())
