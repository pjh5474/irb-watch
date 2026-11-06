"""Saved browser logins, one per (user, site).

A person logs in once in a visible browser (scripts/save_login.py); Playwright's
`storage_state` JSON is stored here and its cookies are handed to SafeFetcher.
The store is a small protocol so a Supabase-backed one can replace the file store later.
"""

import json
import re
from pathlib import Path
from typing import Any, Protocol

import httpx

from irb_watch.crawlers.errors import LoginExpiredError

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class SessionStore(Protocol):
    def load(self, user: str, site: str) -> dict[str, Any] | None: ...

    def save(self, user: str, site: str, state: dict[str, Any]) -> None: ...


def valid_name(value: str) -> bool:
    return _NAME.fullmatch(value) is not None and ".." not in value


def _check_name(label: str, value: str) -> str:
    if not valid_name(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


class FileSessionStore:
    """data/sessions/<user>/<site>.json. Local prototype only; the files hold live cookies."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, user: str, site: str) -> Path:
        return self._root / _check_name("user", user) / f"{_check_name('site', site)}.json"

    def load(self, user: str, site: str) -> dict[str, Any] | None:
        path = self._path(user, site)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, user: str, site: str, state: dict[str, Any]) -> None:
        path = self._path(user, site)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


class MemorySessionStore:
    """Keeps sessions only for this process: scans log in fresh and leave nothing on disk."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], dict[str, Any]] = {}

    def load(self, user: str, site: str) -> dict[str, Any] | None:
        return self._states.get((_check_name("user", user), _check_name("site", site)))

    def save(self, user: str, site: str, state: dict[str, Any]) -> None:
        self._states[(_check_name("user", user), _check_name("site", site))] = state


def cookies_from_state(state: dict[str, Any]) -> httpx.Cookies:
    """Domain-scoped cookies, so a saved login is never sent to another host."""
    jar = httpx.Cookies()
    for c in state.get("cookies", []):
        jar.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
    return jar


def load_cookies(store: SessionStore, user: str, site: str) -> httpx.Cookies:
    """Cookies for (user, site). A missing or empty session is a login failure with a reason."""
    state = store.load(user, site)
    if not state or not state.get("cookies"):
        raise LoginExpiredError(f"no saved login for user '{user}' on site '{site}'")
    return cookies_from_state(state)
