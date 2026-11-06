"""Stored site logins (ID + password), one per (user, site).

The prototype keeps them in Windows Credential Manager through `keyring`. They never go
in the repo, in data/, or in logs. A hosted server needs a different store behind the
same protocol (encrypted in the database).
"""

import json
from dataclasses import dataclass
from typing import Protocol

import keyring

SERVICE = "irb-watch"


@dataclass(frozen=True)
class Credentials:
    login_id: str
    password: str

    def __repr__(self) -> str:  # never leak the password through logs or tracebacks
        return f"Credentials(login_id={self.login_id!r}, password='***')"


class CredentialStore(Protocol):
    def get(self, user: str, site: str) -> Credentials | None: ...

    def set(self, user: str, site: str, credentials: Credentials) -> None: ...


def _key(user: str, site: str) -> str:
    return f"{user}/{site}"


class KeyringCredentialStore:
    def get(self, user: str, site: str) -> Credentials | None:
        raw = keyring.get_password(SERVICE, _key(user, site))
        if raw is None:
            return None
        data = json.loads(raw)
        return Credentials(data["id"], data["password"])

    def set(self, user: str, site: str, credentials: Credentials) -> None:
        raw = json.dumps({"id": credentials.login_id, "password": credentials.password})
        keyring.set_password(SERVICE, _key(user, site), raw)
