"""Supabase-backed store for ALL-SEARCH (services/session_all.py)."""

from datetime import datetime

from postgrest.exceptions import APIError
from supabase import Client

from irb_watch.db.documents import SupabaseSourceStore
from irb_watch.db.paging import chunks, paged
from irb_watch.services.draft_jobs import OPEN
from irb_watch.services.session_all import Candidate, JobTakenError, SearchJob
from irb_watch.services.sessions import POST_CHARS, PostBrief, hospital_board
from irb_watch.services.sources import Source

UNIQUE_VIOLATION = "23505"
_COLUMNS = "id, hospital_id, user_id, stage, status, stop_requested, error, progress, created_at, started_at, finished_at"


def _job(r: dict) -> SearchJob:
    return SearchJob(
        id=r["id"], hospital_id=int(r["hospital_id"]), user_id=r["user_id"], stage=r["stage"], status=r["status"],
        created_at=r["created_at"], stop_requested=bool(r.get("stop_requested")), error=r.get("error"),
        progress=r.get("progress") or {}, started_at=r.get("started_at"), finished_at=r.get("finished_at"),
    )


class SupabaseAllSearchStore:
    def __init__(self, client: Client, runner: str = "dev") -> None:
        self._c = client
        self._runner = runner

    def boards(self, hospital_id: int) -> list[Source]:
        return sorted(
            (
                s for s in SupabaseSourceStore(self._c).list()
                if s.hospital_id == hospital_id and s.is_active and not s.is_manual
                and hospital_board({"adapter": s.adapter, "requires_login": s.requires_login})
            ),
            key=lambda s: s.id,
        )

    def candidates(self, hospital_id: int) -> list[Candidate]:
        boards = {s.id: s for s in self.boards(hospital_id) if s.ai_enabled}
        if not boards:
            return []
        rows: list[dict] = []
        for part in chunks(sorted(boards)):
            rows += paged(
                lambda part=part: self._c.table("documents")
                .select("id, title, posted_on, latest_version_id, source_id")
                .in_("source_id", part).neq("status", "removed").not_.is_("latest_version_id", "null")
                .order("id", desc=True)
            )
        rows.sort(key=lambda r: (r.get("posted_on") or "", int(r["id"])), reverse=True)
        return [
            Candidate(
                PostBrief(int(r["id"]), int(r["latest_version_id"]), r.get("title") or "", r.get("posted_on")),
                boards[int(r["source_id"])].board_kind,
            )
            for r in rows
        ]

    def chars_of(self, version_ids: list[int]) -> dict[int, int]:
        out: dict[int, int] = {}
        for part in chunks(sorted(set(version_ids))):
            for v in self._c.table("document_versions").select("id, body_text").in_("id", part).execute().data:
                out[int(v["id"])] = len(v.get("body_text") or "")
            # Paged: a version with many unpacked zip members has more rows than one answer holds.
            files = paged(
                lambda part=part: self._c.table("document_version_files")
                .select("id, version_id, text_content").in_("version_id", part).order("id")
            )
            for f in files:
                out[int(f["version_id"])] = out.get(int(f["version_id"]), 0) + len(f.get("text_content") or "")
        return {k: min(v, POST_CHARS) for k, v in out.items()}

    def create_job(self, hospital_id: int, user_id: str, stage: str) -> SearchJob:
        try:
            rows = (
                self._c.table("session_search_jobs")
                .insert({"hospital_id": hospital_id, "user_id": user_id, "stage": stage, "runner": self._runner})
                .execute().data
            )
        except APIError as exc:
            if exc.code == UNIQUE_VIOLATION:
                raise JobTakenError from exc
            raise
        return _job(rows[0])

    def get_job(self, job_id: str) -> SearchJob | None:
        rows = self._c.table("session_search_jobs").select(_COLUMNS).eq("id", job_id).limit(1).execute().data
        return _job(rows[0]) if rows else None

    def jobs(self, hospital_id: int, limit: int) -> list[SearchJob]:
        rows = (
            self._c.table("session_search_jobs").select(_COLUMNS).eq("hospital_id", hospital_id)
            .order("created_at", desc=True).limit(limit).execute().data
        )
        return [_job(r) for r in rows]

    def update_job(self, job_id: str, values: dict) -> None:
        # Only an open job changes: one failed at a restart is never turned into "done" later.
        self._c.table("session_search_jobs").update(values).eq("id", job_id).in_("status", list(OPEN)).execute()

    def request_stop(self, job_id: str) -> bool:
        rows = (
            self._c.table("session_search_jobs").update({"stop_requested": True})
            .eq("id", job_id).in_("status", list(OPEN)).execute().data
        )
        return bool(rows)

    def fail_open_jobs(self, error: str, now: datetime) -> int:
        rows = (
            self._c.table("session_search_jobs")
            .update({"status": "failed", "error": error, "finished_at": now.isoformat()})
            .in_("status", list(OPEN)).eq("runner", self._runner).execute().data
        )
        return len(rows or [])

    def purge_jobs(self, cutoff: datetime) -> int:
        rows = (
            self._c.table("session_search_jobs").delete().not_.in_("status", list(OPEN))
            .lt("created_at", cutoff.isoformat()).execute().data
        )
        return len(rows or [])
