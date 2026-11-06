"""Supabase-backed storage for proofs of access to members-only boards."""

from datetime import datetime

from supabase import Client

from irb_watch.db.documents import SupabaseSourceStore
from irb_watch.services.sources import Source


class SupabaseAccessStore:
    def __init__(self, client: Client) -> None:
        self._c = client

    def get_source(self, source_id: int) -> Source | None:
        return SupabaseSourceStore(self._c).get(source_id)

    def grant(
        self, user_id: str, source_id: int, verified_at: datetime, expires_at: datetime
    ) -> None:
        self._c.table("source_access").upsert(
            {
                "user_id": user_id,
                "source_id": source_id,
                "verified_at": verified_at.isoformat(),
                "expires_at": expires_at.isoformat(),
            },
            on_conflict="user_id,source_id",
        ).execute()

    def count_attempts_since(self, user_id: str, since: datetime) -> int:
        result = (
            self._c.table("audit_logs")
            .select("id", count="exact")
            .eq("actor_id", user_id)
            .eq("event_type", "access.attempted")
            .gte("created_at", since.isoformat())
            .execute()
        )
        return result.count or 0
