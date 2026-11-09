"""Supabase-backed state for the email alarms."""

from postgrest.exceptions import APIError
from supabase import Client

UNIQUE_VIOLATION = "23505"


class SupabaseAlertStore:
    def __init__(self, client: Client) -> None:
        self._c = client

    # -- what admins were last told about each source --------------------------------------

    def get_levels(self) -> dict[int, str]:
        rows = self._c.table("source_alert_state").select("source_id, notified_level").execute().data
        return {r["source_id"]: r["notified_level"] for r in rows}

    def set_level(self, source_id: int, level: str, reason: str | None, at: str) -> None:
        self._c.table("source_alert_state").upsert(
            {
                "source_id": source_id,
                "notified_level": level,
                "notified_reason": reason,
                "notified_at": at,
            },
            on_conflict="source_id",
        ).execute()

    def clear_level(self, source_id: int) -> None:
        self._c.table("source_alert_state").delete().eq("source_id", source_id).execute()

    # -- mails that may be sent only once ----------------------------------------------------

    def claim(self, key: str) -> bool:
        """True if this call got the key; False if someone already holds it."""
        try:
            self._c.table("mail_claims").insert({"key": key}).execute()
        except APIError as exc:
            if exc.code == UNIQUE_VIOLATION:
                return False
            raise
        return True

    def release(self, key: str) -> None:
        self._c.table("mail_claims").delete().eq("key", key).execute()

    # -- who gets admin mail -------------------------------------------------------------------

    def admin_emails(self) -> tuple[str, ...]:
        rows = (
            self._c.table("profiles")
            .select("email")
            .eq("role", "admin")
            .is_("deleted_at", "null")
            .execute()
            .data
        )
        return tuple(sorted({r["email"] for r in rows if r.get("email")}))
