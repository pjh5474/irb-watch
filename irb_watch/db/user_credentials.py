"""A user's own logins stored encrypted in the database (table user_site_credentials).

Like db/credentials.py: the table holds only ciphertext, and the plaintext repeats the row's own
(user_id, site) so a ciphertext copied into another row is refused. Unlike it, nothing here is
reachable from the admin API, and no function lists logins. The password never appears in a log
line or an error message.
"""

import json
from datetime import UTC, datetime

from supabase import Client

from irb_watch.crawlers.credentials import Credentials
from irb_watch.crawlers.errors import CredentialStoreError
from irb_watch.security.secretbox import SecretBox, SecretBoxError
from irb_watch.services.user_logins import UserLogin

_META = (
    "user_id, site, login_hint, status, last_error, last_tested_at, last_ok_at, "
    "test_window_start, test_count, reject_count, ciphertext"
)


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class SupabaseUserLoginStore:
    def __init__(self, client: Client, keys: str) -> None:
        self._c = client
        self._keys = keys  # checked when first used, so a missing key is reported with a reason

    def _box(self) -> SecretBox:
        try:
            return SecretBox(self._keys)
        except SecretBoxError as exc:
            raise CredentialStoreError(str(exc)) from None

    def _select(self, columns: str, user_id: str, site: str) -> list[dict]:
        try:
            return (
                self._c.table("user_site_credentials").select(columns)
                .eq("user_id", user_id).eq("site", site).limit(1).execute().data
            )
        except Exception as exc:  # noqa: BLE001  (cause dropped on purpose: it may name secrets)
            raise CredentialStoreError(f"could not read the saved login ({type(exc).__name__})") from None

    def get(self, user_id: str, site: str) -> UserLogin | None:
        rows = self._select(_META, user_id, site)
        if not rows:
            return None
        r = rows[0]
        return UserLogin(
            user_id=r["user_id"], site=r["site"], has_login=r["ciphertext"] is not None,
            login_hint=r["login_hint"], status=r["status"], last_error=r["last_error"],
            last_tested_at=_when(r["last_tested_at"]), last_ok_at=_when(r["last_ok_at"]),
            test_window_start=_when(r["test_window_start"]), test_count=r["test_count"],
            reject_count=r["reject_count"],
        )

    def get_credentials(self, user_id: str, site: str) -> Credentials | None:
        rows = self._select("ciphertext", user_id, site)
        if not rows or rows[0]["ciphertext"] is None:
            return None
        try:
            data = json.loads(self._box().decrypt(rows[0]["ciphertext"]))
        except SecretBoxError as exc:
            raise CredentialStoreError(str(exc)) from None
        if data.get("owner") != user_id or data.get("site") != site:
            raise CredentialStoreError("the saved login does not belong to this row (data was moved or changed)")
        return Credentials(data["id"], data["password"])

    def ensure_ready(self) -> None:
        self._box()  # raises CredentialStoreError with the reason when the key is missing or bad

    def put_secret(
        self, user_id: str, site: str, credentials: Credentials, hint: str, fields: dict
    ) -> bool:
        payload = json.dumps(
            {"owner": user_id, "site": site, "id": credentials.login_id, "password": credentials.password}
        )
        values = {"ciphertext": self._box().encrypt(payload), "login_hint": hint, **fields}
        table = self._c.table("user_site_credentials")
        try:
            # One statement decides: update only when no test is running. No row updated means
            # either no row yet (insert it) or a test is running (refuse).
            if table.update(values).eq("user_id", user_id).eq("site", site).neq("status", "testing").execute().data:
                return True
            if table.select("user_id").eq("user_id", user_id).eq("site", site).limit(1).execute().data:
                return False
            try:
                table.insert({"user_id": user_id, "site": site, **values}).execute()
            except Exception as exc:
                if getattr(exc, "code", None) == "23505":  # a request of the same user won the insert
                    return False
                raise
            return True
        except Exception as exc:  # noqa: BLE001  (cause dropped on purpose: it may echo the row)
            raise CredentialStoreError(f"could not save the login ({type(exc).__name__})") from None

    def update_if_saved(
        self, user_id: str, site: str, fields: dict, *,
        not_testing: bool = False, only_if_tested_at: datetime | None = None,
    ) -> bool:
        try:
            query = (
                self._c.table("user_site_credentials").update(fields)
                .eq("user_id", user_id).eq("site", site).not_.is_("ciphertext", "null")
            )
            if not_testing:
                query = query.neq("status", "testing")
            if only_if_tested_at is not None:
                query = query.eq("last_tested_at", only_if_tested_at.isoformat())
            rows = query.execute().data
        except Exception as exc:  # noqa: BLE001
            raise CredentialStoreError(f"could not update the login ({type(exc).__name__})") from None
        return bool(rows)

    def add_rejection(self, user_id: str, site: str, reason: str, tested_at: datetime | None) -> bool:
        try:
            result = self._c.rpc(
                "user_login_rejected",
                {"p_user": user_id, "p_site": site, "p_reason": reason[:300],
                 "p_tested_at": tested_at.isoformat() if tested_at else None},
            ).execute().data
        except Exception as exc:  # noqa: BLE001
            raise CredentialStoreError(f"could not record the refusal ({type(exc).__name__})") from None
        return bool(result)

    def clear(self, user_id: str, site: str) -> bool:
        try:
            rows = (
                self._c.table("user_site_credentials")
                .update({"ciphertext": None, "login_hint": None, "status": "untested", "last_error": None,
                         "updated_at": datetime.now(UTC).isoformat()})
                .eq("user_id", user_id).eq("site", site).not_.is_("ciphertext", "null")
                .execute().data
            )
        except Exception as exc:  # noqa: BLE001
            raise CredentialStoreError(f"could not delete the login ({type(exc).__name__})") from None
        return bool(rows)

    def fail_testing(self, reason: str) -> int:
        try:
            rows = (
                self._c.table("user_site_credentials")
                .update({"status": "failed", "last_error": reason[:300],
                         "updated_at": datetime.now(UTC).isoformat()})
                .eq("status", "testing").execute().data
            )
        except Exception as exc:  # noqa: BLE001
            raise CredentialStoreError(f"could not clear unfinished tests ({type(exc).__name__})") from None
        return len(rows)
