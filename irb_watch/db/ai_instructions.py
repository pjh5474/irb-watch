"""Supabase-backed storage of the admin-edited AI instructions."""

from datetime import datetime

from supabase import Client

from irb_watch.services.ai_instructions import InstructionVersion


def _row(r: dict) -> InstructionVersion:
    return InstructionVersion(
        id=r["id"],
        feature=r["feature"],
        body=r["body"] or "",
        created_by=r.get("created_by"),
        created_at=r["created_at"],
        restored_from=r.get("restored_from"),
    )


class SupabaseInstructionsStore:
    def __init__(self, client: Client) -> None:
        self._c = client

    def latest(self, feature: str) -> InstructionVersion | None:
        rows = (
            self._c.table("ai_instructions")
            .select("*")
            .eq("feature", feature)
            .order("id", desc=True)
            .limit(1)
            .execute()
            .data
        )
        return _row(rows[0]) if rows else None

    def history(self, feature: str, limit: int) -> list[InstructionVersion]:
        rows = (
            self._c.table("ai_instructions")
            .select("*")
            .eq("feature", feature)
            .order("id", desc=True)
            .limit(limit)
            .execute()
            .data
        )
        return [_row(r) for r in rows]

    def get(self, version_id: int) -> InstructionVersion | None:
        rows = self._c.table("ai_instructions").select("*").eq("id", version_id).limit(1).execute().data
        return _row(rows[0]) if rows else None

    def add(self, feature, body, created_by, restored_from, now: datetime) -> InstructionVersion:
        rows = (
            self._c.table("ai_instructions")
            .insert(
                {
                    "feature": feature,
                    "body": body,
                    "created_by": created_by,
                    "restored_from": restored_from,
                    "created_at": now.isoformat(),
                }
            )
            .execute()
            .data
        )
        return _row(rows[0])

    def count_tests_since(self, admin_id: str, since: datetime) -> int:
        result = (
            self._c.table("audit_logs")
            .select("id", count="exact")
            .eq("actor_id", admin_id)
            .eq("event_type", "ai_instructions.tested")
            .gte("created_at", since.isoformat())
            .limit(1)
            .execute()
        )
        return result.count or 0
