"""Supabase-backed reads and writes for the daily digest and the unsubscribe link."""

from irb_watch.db.alerts import SupabaseAlertStore
from irb_watch.services.digest import Card, DigestUser, SourceInfo

CARD_EVENTS = ["document.new", "document.updated", "document.reappeared"]


class SupabaseDigestStore(SupabaseAlertStore):
    """Also gives claim() and release() (the once-only keys) from the alert store."""

    def users(self) -> list[DigestUser]:
        profiles = (
            self._c.table("profiles")
            .select("id, email, role")
            .is_("deleted_at", "null")
            .execute()
            .data
        )
        prefs = {
            r["user_id"]: r
            for r in self._c.table("notification_prefs")
            # step_reminders_enabled: migration 20261113024818_step_reminders (apply before deploy)
            .select("user_id, digest_enabled, last_digest_at, step_reminders_enabled")
            .execute()
            .data
        }
        return [
            DigestUser(
                id=p["id"],
                email=p["email"],
                role=p["role"],
                enabled=prefs.get(p["id"], {}).get("digest_enabled", True),  # no row = on
                last_digest_at=prefs.get(p["id"], {}).get("last_digest_at"),
                reminders_enabled=prefs.get(p["id"], {}).get("step_reminders_enabled", True),
            )
            for p in profiles
            if p.get("email")
        ]

    def sources(self) -> list[SourceInfo]:
        rows = self._c.table("sources").select("id, name, requires_login").execute().data
        return [SourceInfo(r["id"], r["name"], bool(r["requires_login"])) for r in rows]

    def memberships(self) -> dict[str, set[int]]:
        rows = self._c.table("source_members").select("source_id, user_id").execute().data
        result: dict[str, set[int]] = {}
        for r in rows:
            result.setdefault(r["user_id"], set()).add(r["source_id"])
        return result

    def cards_since(self, since: str, limit: int) -> list[Card]:
        rows = (
            self._c.table("audit_logs")
            .select("created_at, event_type, source_id, document_id, title")
            .eq("category", "document")
            .in_("event_type", CARD_EVENTS)
            .gt("created_at", since)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )
        return [
            Card(r["created_at"], r["event_type"], r["source_id"], r["document_id"], r["title"])
            for r in rows
            if r.get("source_id") is not None
        ]

    def set_cursor(self, user_id: str, at: str) -> None:
        # Only the given columns are written, so a user's own digest_enabled choice is kept.
        self._c.table("notification_prefs").upsert(
            {"user_id": user_id, "last_digest_at": at, "updated_at": at}, on_conflict="user_id"
        ).execute()

    def set_digest_enabled(self, user_id: str, enabled: bool, at: str) -> None:
        self._c.table("notification_prefs").upsert(
            {"user_id": user_id, "digest_enabled": enabled, "updated_at": at}, on_conflict="user_id"
        ).execute()

    def set_reminders_enabled(self, user_id: str, enabled: bool, at: str) -> None:
        self._c.table("notification_prefs").upsert(
            {"user_id": user_id, "step_reminders_enabled": enabled, "updated_at": at}, on_conflict="user_id"
        ).execute()
