"""Hospital logins stored encrypted in the database (CREDENTIAL_STORE=database).

The table holds only ciphertext. The plaintext is a small JSON that also repeats the row's
own (owner_label, site), so a ciphertext copied into another row is refused instead of being
used for the wrong site. The password never appears in a log line or an error message.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from supabase import Client

from irb_watch.crawlers.credentials import Credentials
from irb_watch.crawlers.errors import CredentialStoreError
from irb_watch.security.secretbox import SecretBox, SecretBoxError


@dataclass(frozen=True)
class CredentialEntry:
    owner_label: str
    site: str
    updated_at: str


class DatabaseCredentialStore:
    def __init__(self, client: Client, keys: str) -> None:
        self._c = client
        self._keys = keys  # checked when first used, so a missing key is reported with a reason

    def _box(self) -> SecretBox:
        try:
            return SecretBox(self._keys)
        except SecretBoxError as exc:
            raise CredentialStoreError(str(exc)) from None

    def get(self, user: str, site: str) -> Credentials | None:
        try:
            rows = (
                self._c.table("site_credentials")
                .select("ciphertext")
                .eq("owner_label", user)
                .eq("site", site)
                .limit(1)
                .execute()
                .data
            )
        except Exception as exc:  # noqa: BLE001  (cause dropped on purpose: it may name secrets)
            raise CredentialStoreError(
                f"could not read stored credentials ({type(exc).__name__})"
            ) from None
        if not rows:
            return None
        try:
            data = json.loads(self._box().decrypt(rows[0]["ciphertext"]))
        except SecretBoxError as exc:
            raise CredentialStoreError(str(exc)) from None
        if data.get("owner") != user or data.get("site") != site:
            raise CredentialStoreError(
                "stored credentials do not belong to this row (data was moved or changed)"
            )
        return Credentials(data["id"], data["password"])

    def set(
        self, user: str, site: str, credentials: Credentials, updated_by: str | None = None
    ) -> None:
        payload = json.dumps(
            {
                "owner": user,
                "site": site,
                "id": credentials.login_id,
                "password": credentials.password,
            }
        )
        self._c.table("site_credentials").upsert(
            {
                "owner_label": user,
                "site": site,
                "ciphertext": self._box().encrypt(payload),
                "updated_at": datetime.now(UTC).isoformat(),
                "updated_by": updated_by,
            },
            on_conflict="owner_label,site",
        ).execute()

    def list_entries(self) -> list[CredentialEntry]:
        """Labels and dates only. Never the ciphertext, never the login."""
        rows = (
            self._c.table("site_credentials")
            .select("owner_label, site, updated_at")
            .order("owner_label")
            .order("site")
            .execute()
            .data
        )
        return [CredentialEntry(r["owner_label"], r["site"], r["updated_at"]) for r in rows]

    def delete(self, user: str, site: str) -> bool:
        rows = (
            self._c.table("site_credentials")
            .delete()
            .eq("owner_label", user)
            .eq("site", site)
            .execute()
            .data
        )
        return bool(rows)
