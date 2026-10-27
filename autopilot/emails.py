"""The Zoom accounts in the page's dropdown, and which of them the autopilot
watches.

Kept in storage_dir/state/emails.json (with its backup copy), so an address
added on the page is there for everyone, in every browser, after restarts.
The default account (ZOOM_DEFAULT_EMAIL, PM) is always first, can't be
removed, and starts with the autopilot on.
"""

from __future__ import annotations

import re
import threading

from core.config import get_settings
from ledger import files

FILE = "emails.json"
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_lock = threading.Lock()


def _path():
    return files.state_dir() / FILE


def _clean(email: str) -> str:
    return (email or "").strip().lower()


def _load() -> list[dict]:
    settings = get_settings()
    default = _clean(settings.zoom_default_email)
    data = files.read_json(_path(), None)
    rows = []
    if isinstance(data, dict) and isinstance(data.get("emails"), list):
        for row in data["emails"]:
            if isinstance(row, dict) and _EMAIL.fullmatch(_clean(row.get("email", ""))):
                rows.append({"email": _clean(row["email"]), "autopilot": bool(row.get("autopilot"))})
    else:  # first run: the accounts the settings name
        rows = [{"email": default, "autopilot": True}]
        extra = [*settings.zoom_emails, settings.zoom_host_email]
        rows += [{"email": _clean(e), "autopilot": False} for e in extra if _EMAIL.fullmatch(_clean(e))]
    # the default is always there and always first, each address once
    if default and not any(r["email"] == default for r in rows):
        rows.insert(0, {"email": default, "autopilot": True})
    rows.sort(key=lambda r: r["email"] != default)
    seen, unique = set(), []
    for row in rows:
        if row["email"] not in seen:
            seen.add(row["email"])
            unique.append(row)
    return unique


def _save(rows: list[dict]) -> None:
    files.write_json(_path(), {"emails": rows})


def list_emails() -> list[dict]:
    """[{"email", "autopilot", "default"}], the default first."""
    default = _clean(get_settings().zoom_default_email)
    with _lock:
        return [{**row, "default": row["email"] == default} for row in _load()]


def autopilot_emails() -> list[str]:
    return [row["email"] for row in list_emails() if row["autopilot"]]


def add_email(email: str) -> list[dict]:
    email = _clean(email)
    if len(email) > 254 or not _EMAIL.fullmatch(email):
        raise ValueError("That doesn't look like an email address.")
    with _lock:
        rows = _load()
        if not any(r["email"] == email for r in rows):
            rows.append({"email": email, "autopilot": False})
            _save(rows)
    return list_emails()


def remove_email(email: str) -> list[dict]:
    email = _clean(email)
    if email == _clean(get_settings().zoom_default_email):
        raise ValueError("The default account can't be removed.")
    with _lock:
        rows = _load()
        kept = [r for r in rows if r["email"] != email]
        if len(kept) == len(rows):
            raise KeyError(email)
        _save(kept)
    return list_emails()


def set_autopilot(email: str, enabled: bool) -> list[dict]:
    email = _clean(email)
    with _lock:
        rows = _load()
        for row in rows:
            if row["email"] == email:
                row["autopilot"] = bool(enabled)
                break
        else:
            raise KeyError(email)
        _save(rows)
    return list_emails()
