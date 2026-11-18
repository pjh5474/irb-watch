"""Supabase-backed store for the daily AI date search (services/session_daily.py)."""

from supabase import Client

from irb_watch.db.paging import chunks, paged
from irb_watch.services.session_daily import SearchState
from irb_watch.services.sessions import PostBrief, hospital_board

_COLUMNS = "hospital_id, last_run_at, last_ok_at, last_status, last_error, last_posts, last_proposed"


class SupabaseDailySearchStore:
    def __init__(self, client: Client) -> None:
        self._c = client

    def notice_sources(self, source_ids: list[int]) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        for part in chunks(sorted(set(source_ids))):
            rows = (
                self._c.table("sources")
                .select("id, hospital_id, adapter, requires_login")
                .in_("id", part).eq("board_kind", "notice").eq("ai_enabled", True).eq("is_active", True)
                .not_.is_("hospital_id", "null")
                .execute().data
            )
            for r in rows:
                if hospital_board(r):
                    out.setdefault(int(r["hospital_id"]), []).append(int(r["id"]))
        return out

    def changed_posts(self, source_ids: list[int], since: str, limit: int) -> list[PostBrief]:
        # The versions saved after `since` (the database compares the times, so no string formats
        # are compared here), then the live posts of these sources whose latest version is one of
        # them. Not a "newest N documents" window: an old post that got a new version is found.
        created: dict[int, str] = {
            int(v["id"]): v["created_at"]
            for v in paged(
                # Not the silent versions of a first scan or of an ALL-SEARCH backlog read: those
                # are left to the admin's ALL-SEARCH, never read again as "changed".
                lambda: self._c.table("document_versions").select("id, created_at")
                .gt("created_at", since).not_.in_("change_type", ["baseline", "resaved"]).order("id")
            )
        }
        docs: list[dict] = []
        for part in chunks(sorted(created)):
            for sources in chunks(sorted(set(source_ids))):
                docs += (
                    self._c.table("documents").select("id, title, posted_on, latest_version_id")
                    .in_("source_id", sources).in_("latest_version_id", part)
                    .neq("status", "removed").execute().data
                )
        fresh = sorted(docs, key=lambda d: created[int(d["latest_version_id"])], reverse=True)
        return [
            PostBrief(int(d["id"]), int(d["latest_version_id"]), d.get("title") or "", d.get("posted_on"))
            for d in fresh[:limit]
        ]

    def state(self, hospital_id: int) -> SearchState | None:
        rows = self._c.table("hospital_session_search").select(_COLUMNS).eq("hospital_id", hospital_id).limit(1).execute().data
        if not rows:
            return None
        r = rows[0]
        return SearchState(
            int(r["hospital_id"]), r.get("last_run_at"), r.get("last_ok_at"), r.get("last_status"),
            r.get("last_error"), int(r.get("last_posts") or 0), int(r.get("last_proposed") or 0),
        )

    def save_state(self, state: SearchState) -> None:
        self._c.table("hospital_session_search").upsert(
            {
                "hospital_id": state.hospital_id, "last_run_at": state.last_run_at, "last_ok_at": state.last_ok_at,
                "last_status": state.last_status, "last_error": state.last_error,
                "last_posts": state.last_posts, "last_proposed": state.last_proposed,
                "updated_at": state.last_run_at,
            },
            on_conflict="hospital_id",
        ).execute()
