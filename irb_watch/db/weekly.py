"""Supabase-backed reads for the weekly stats mail."""

from datetime import UTC, datetime

from irb_watch.core.weekly import Event, UserInfo
from irb_watch.db.alerts import SupabaseAlertStore

MAX_EVENTS = 20_000
PAGE = 200
MAX_PAGES = 5  # 1,000 accounts


def _dt(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class SupabaseWeeklyStore(SupabaseAlertStore):
    """Also gives claim(), release() and admin_emails() from the alert store."""

    def events(self, types: list[str], start: datetime, end: datetime) -> list[Event]:
        rows = (
            self._c.table("audit_logs")
            .select("created_at, event_type, actor_id, source_id, payload")
            .in_("event_type", types)
            .gte("created_at", start.isoformat())
            .lt("created_at", end.isoformat())
            .order("created_at", desc=True)
            .limit(MAX_EVENTS)
            .execute()
            .data
        )
        events = []
        for r in rows:
            at = _dt(r["created_at"])
            if at is not None:
                events.append(Event(r["event_type"], r.get("actor_id"), r.get("source_id"), at, r.get("payload") or {}))
        return events

    def users(self) -> list[UserInfo]:
        """Active accounts with the dates only the auth service knows (last sign-in)."""
        profiles = {
            p["id"]: p
            for p in self._c.table("profiles")
            .select("id, email")
            .is_("deleted_at", "null")
            .execute()
            .data
        }
        result = []
        for page in range(1, MAX_PAGES + 1):
            batch = self._c.auth.admin.list_users(page=page, per_page=PAGE)
            for user in batch:
                profile = profiles.get(str(user.id))
                if profile is None:  # deleted or unknown accounts are not part of the report
                    continue
                result.append(
                    UserInfo(
                        id=str(user.id),
                        email=profile.get("email") or user.email,
                        created_at=_dt(user.created_at),
                        last_sign_in_at=_dt(getattr(user, "last_sign_in_at", None)),
                    )
                )
            if len(batch) < PAGE:
                break
        return result
