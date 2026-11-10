"""Supabase-backed store for documents needing review (service role)."""

from datetime import datetime

from supabase import Client

from irb_watch.db.documents import version_files
from irb_watch.services.reviews import ReviewRow, settled_values, settles


class SupabaseReviewStore:
    _COLUMNS = "id, source_id, title, status, review_reason, latest_version_id"

    def __init__(self, client: Client) -> None:
        self._c = client

    def _rows(self, docs: list[dict]) -> list[ReviewRow]:
        if not docs:
            return []
        source_ids = sorted({d["source_id"] for d in docs})
        names = {
            s["id"]: s["name"]
            for s in self._c.table("sources").select("id, name").in_("id", source_ids).execute().data
        }
        files = version_files(self._c, [d["latest_version_id"] for d in docs if d["latest_version_id"]])
        return [
            ReviewRow(
                document_id=d["id"],
                source_id=d["source_id"],
                source_name=names.get(d["source_id"], f"source #{d['source_id']}"),
                title=d["title"],
                status=d["status"],
                review_reason=d["review_reason"],
                files=files.get(d["latest_version_id"], ()),
            )
            for d in docs
        ]

    def needing_review(self, limit: int) -> list[ReviewRow]:
        docs = (
            self._c.table("documents")
            .select(self._COLUMNS)
            .eq("status", "needs_review")
            .order("id", desc=True)
            .limit(limit)
            .execute()
            .data
        )
        return self._rows(docs)

    def get(self, document_id: int) -> ReviewRow | None:
        docs = self._c.table("documents").select(self._COLUMNS).eq("id", document_id).execute().data
        rows = self._rows(docs)
        return rows[0] if rows else None

    def acknowledge(
        self, document_id: int, reason: str, key: str, admin_id: str, now: datetime
    ) -> bool:
        # Conditional: only while the document still has exactly this reason, so a scan that
        # ran in between (new warning) is never cleared by an OK given for the old one.
        cleared = (
            self._c.table("documents")
            .update({"status": "current", "review_reason": None})
            .eq("id", document_id)
            .eq("status", "needs_review")
            .eq("review_reason", reason)
            .execute()
            .data
        )
        if not cleared:
            return False
        try:
            self._c.table("document_reviews").upsert(
                {
                    "document_id": document_id,
                    "ack_key": key,
                    "reason": reason,
                    "acknowledged_by": admin_id,
                    "acknowledged_at": now.isoformat(),
                }
            ).execute()
        except Exception:
            self._restore(document_id, reason)
            raise
        return True

    def settle_source(self, source_id: int) -> None:
        rows = (
            self._c.table("sources")
            .select("last_scan_status, last_scan_error, scans_paused_reason")
            .eq("id", source_id)
            .execute()
            .data
        )
        if not rows:
            return
        source = rows[0]
        error = source["last_scan_error"] or ""
        if not settles(source["last_scan_status"], error, source["scans_paused_reason"]):
            return  # a whole-scan problem or nothing to settle: leave it to the next scan
        left = (
            self._c.table("documents")
            .select("external_id, review_reason")
            .eq("source_id", source_id)
            .eq("status", "needs_review")
            .order("id")
            .execute()
            .data
        )
        values = settled_values([(d["external_id"], d["review_reason"] or "") for d in left])
        # Guarded: if a scan changed the source meanwhile, its own result stands.
        (
            self._c.table("sources")
            .update(values)
            .eq("id", source_id)
            .eq("last_scan_status", "needs_review")
            .eq("last_scan_error", error)
            .execute()
        )

    def undo(self, document_id: int, reason: str) -> None:
        self._c.table("document_reviews").delete().eq("document_id", document_id).execute()
        self._restore(document_id, reason)

    def _restore(self, document_id: int, reason: str) -> None:
        # Back to needs_review only if nothing else changed it since (a scan may have run).
        (
            self._c.table("documents")
            .update({"status": "needs_review", "review_reason": reason})
            .eq("id", document_id)
            .eq("status", "current")
            .is_("review_reason", "null")
            .execute()
        )
