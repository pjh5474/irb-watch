"""Supabase-backed list of the readers assigned to members-only sources."""

from supabase import Client

from irb_watch.services.members import Member


class SupabaseMemberStore:
    def __init__(self, client: Client) -> None:
        self._c = client

    def list(self, source_id: int) -> list[Member]:
        rows = (
            self._c.table("source_members")
            .select("user_id, added_at")
            .eq("source_id", source_id)
            .order("added_at")
            .execute()
            .data
        )
        if not rows:
            return []
        profiles = (
            self._c.table("profiles")
            .select("id, email, display_name")
            .in_("id", [r["user_id"] for r in rows])
            .execute()
            .data
        )
        by_id = {p["id"]: p for p in profiles}
        return [
            Member(
                user_id=r["user_id"],
                email=by_id.get(r["user_id"], {}).get("email"),
                display_name=by_id.get(r["user_id"], {}).get("display_name"),
                added_at=r["added_at"],
            )
            for r in rows
        ]

    def add(self, source_id: int, user_id: str, added_by: str | None) -> None:
        self._c.table("source_members").upsert(
            {"source_id": source_id, "user_id": user_id, "added_by": added_by},
            on_conflict="source_id,user_id",
            ignore_duplicates=True,
        ).execute()

    def remove(self, source_id: int, user_id: str) -> bool:
        rows = (
            self._c.table("source_members")
            .delete()
            .eq("source_id", source_id)
            .eq("user_id", user_id)
            .execute()
            .data
        )
        return bool(rows)
