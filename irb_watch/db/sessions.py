"""Supabase-backed store for services.sessions (IRB sessions, kinds and the AI job; CAL-1)."""

from datetime import datetime

from postgrest.exceptions import APIError
from supabase import Client

from irb_watch.core.state import FileContent, VersionContent
from irb_watch.db.paging import chunks, paged
from irb_watch.services.draft_jobs import OPEN
from irb_watch.services.sessions import (
    PROPOSED,
    PostBrief,
    Session,
    SessionJob,
    SessionJobTakenError,
    SessionKind,
    SessionTakenError,
)

UNIQUE_VIOLATION = "23505"
_SESSION_COLUMNS = (
    "id, hospital_id, kind_key, meets_on, meets_at, cutoff_on, cutoff_at, timezone, state, origin, note, "
    "evidence_version_id, evidence_url, evidence_quote, proposed_by, proposed_at, reviewed_by, reviewed_at, "
    "job_id, created_at, updated_at, deleted_at"
)
_JOB_COLUMNS = (
    "id, hospital_id, user_id, status, error, documents_read, proposed, skipped, created_at, started_at, finished_at"
)


def _hhmm(value) -> str | None:
    return str(value)[:5] if value else None


def _session(r: dict) -> Session:
    return Session(
        id=int(r["id"]), hospital_id=int(r["hospital_id"]), kind_key=r["kind_key"],
        meets_on=str(r["meets_on"])[:10], state=r["state"], origin=r["origin"],
        meets_at=_hhmm(r.get("meets_at")), cutoff_on=str(r["cutoff_on"])[:10] if r.get("cutoff_on") else None,
        cutoff_at=_hhmm(r.get("cutoff_at")), timezone=r.get("timezone") or "Asia/Seoul", note=r.get("note") or "",
        evidence_version_id=r.get("evidence_version_id"), evidence_url=r.get("evidence_url"),
        evidence_quote=r.get("evidence_quote"),
        proposed_by=str(r["proposed_by"]) if r.get("proposed_by") else None, proposed_at=r.get("proposed_at"),
        reviewed_by=str(r["reviewed_by"]) if r.get("reviewed_by") else None, reviewed_at=r.get("reviewed_at"),
        job_id=str(r["job_id"]) if r.get("job_id") else None, created_at=r.get("created_at") or "",
        updated_at=r.get("updated_at") or "", deleted_at=r.get("deleted_at"),
    )


def _job(r: dict) -> SessionJob:
    return SessionJob(
        id=str(r["id"]), hospital_id=int(r["hospital_id"]), user_id=str(r["user_id"]), status=r["status"],
        created_at=r["created_at"], error=r.get("error"), documents_read=int(r.get("documents_read") or 0),
        proposed=int(r.get("proposed") or 0), skipped=int(r.get("skipped") or 0),
        started_at=r.get("started_at"), finished_at=r.get("finished_at"),
    )


def _kind(r: dict) -> SessionKind:
    return SessionKind(key=r["key"], label=r["label"], position=int(r["position"]), archived_at=r.get("archived_at"))


class SupabaseSessionStore:
    def __init__(self, client: Client, runner: str = "dev") -> None:
        self._c = client
        self._runner = runner  # this server's name (APP_ENV); see fail_open_jobs

    # kinds
    def kinds(self) -> list[SessionKind]:
        rows = self._c.table("session_kinds").select("key, label, position, archived_at").order("position").execute().data
        return [_kind(r) for r in rows]

    def add_kind(self, key: str, label: str, position: int) -> SessionKind:
        try:
            rows = self._c.table("session_kinds").insert({"key": key, "label": label, "position": position}).execute().data
        except APIError as exc:
            if exc.code == UNIQUE_VIOLATION:
                raise LookupError("taken") from exc
            raise
        return _kind(rows[0])

    def update_kind(self, key: str, values: dict) -> SessionKind:
        rows = self._c.table("session_kinds").update(values).eq("key", key).execute().data
        return _kind(rows[0])

    # sessions
    def get(self, session_id: int) -> Session | None:
        rows = self._c.table("irb_sessions").select(_SESSION_COLUMNS).eq("id", session_id).limit(1).execute().data
        return _session(rows[0]) if rows else None

    def sessions(self, session_ids: list[int]) -> list[Session]:
        ids = sorted(set(session_ids))
        out: list[Session] = []
        for chunk in chunks(ids):
            rows = self._c.table("irb_sessions").select(_SESSION_COLUMNS).in_("id", chunk).execute().data
            out += [_session(r) for r in rows]
        return out

    def between(self, hospital_ids: list[int], start: str, end: str) -> list[Session]:
        ids = sorted(set(hospital_ids))
        found: dict[int, Session] = {}
        for chunk in chunks(ids):
            for column in ("meets_on", "cutoff_on"):
                rows = paged(
                    lambda column=column, chunk=chunk: self._c.table("irb_sessions").select(_SESSION_COLUMNS)
                    .in_("hospital_id", chunk).is_("deleted_at", "null").gte(column, start).lte(column, end).order("id")
                )
                found.update({int(r["id"]): _session(r) for r in rows})
        return list(found.values())

    def same_day(self, hospital_id: int, kind_key: str, meets_on: str) -> list[Session]:
        rows = (
            self._c.table("irb_sessions").select(_SESSION_COLUMNS).eq("hospital_id", hospital_id)
            .eq("kind_key", kind_key).eq("meets_on", meets_on).execute().data
        )
        return [_session(r) for r in rows]

    def proposals(self, limit: int) -> list[Session]:
        rows = (
            self._c.table("irb_sessions").select(_SESSION_COLUMNS).eq("state", PROPOSED).is_("deleted_at", "null")
            .order("created_at").limit(limit).execute().data
        )
        return [_session(r) for r in rows]

    def create(self, values: dict) -> Session:
        try:
            rows = self._c.table("irb_sessions").insert(values).execute().data
        except APIError as exc:
            if exc.code == UNIQUE_VIOLATION:
                raise SessionTakenError from exc
            raise
        return _session(rows[0])

    def update(self, session_id: int, values: dict) -> Session:
        try:
            rows = self._c.table("irb_sessions").update(values).eq("id", session_id).execute().data
        except APIError as exc:
            if exc.code == UNIQUE_VIOLATION:
                raise SessionTakenError from exc
            raise
        if not rows:
            raise LookupError("gone")
        return _session(rows[0])

    def version_hospital(self, version_id: int) -> int | None:
        v = self._c.table("document_versions").select("document_id").eq("id", version_id).limit(1).execute().data
        if not v:
            return None
        d = self._c.table("documents").select("source_id").eq("id", v[0]["document_id"]).limit(1).execute().data
        if not d:
            return None
        s = self._c.table("sources").select("hospital_id").eq("id", d[0]["source_id"]).limit(1).execute().data
        return int(s[0]["hospital_id"]) if s and s[0].get("hospital_id") is not None else None

    # the AI job's input
    def posts_for(self, hospital_id: int, limit: int) -> list[PostBrief]:
        # Every AI-on board, login boards included (owner, 2026-11-17): meeting dates are
        # hospital-level facts, and the AI-on flag is the consent to read the board.
        sources = (
            self._c.table("sources").select("id").eq("hospital_id", hospital_id).eq("ai_enabled", True).execute().data
        )
        ids = [int(r["id"]) for r in sources]
        if not ids:
            return []
        rows = (
            self._c.table("documents").select("id, title, posted_on, latest_version_id")
            .in_("source_id", ids).neq("status", "removed").not_.is_("latest_version_id", "null")
            .order("posted_on", desc=True, nullsfirst=False).order("id", desc=True).limit(limit).execute().data
        )
        return [
            PostBrief(int(r["id"]), int(r["latest_version_id"]), r.get("title") or "", r.get("posted_on"))
            for r in rows
        ]

    def version_content(self, version_id: int) -> VersionContent | None:
        rows = self._c.table("document_versions").select("id, title, body_text").eq("id", version_id).limit(1).execute().data
        if not rows:
            return None
        files = (
            self._c.table("document_version_files")
            .select("filename, size_label, sha256, text_content, hash_only_reason, extract_note")
            .eq("version_id", version_id).order("id").execute().data
        )
        return VersionContent(
            rows[0]["title"] or "",
            rows[0]["body_text"] or "",
            tuple(
                FileContent(
                    filename=f["filename"], size_label=f["size_label"] or "", sha256=f["sha256"],
                    text=f["text_content"], hash_only_reason=f["hash_only_reason"], note=f["extract_note"],
                )
                for f in files
            ),
        )

    # jobs
    def create_job(self, hospital_id: int, user_id: str) -> SessionJob:
        try:
            rows = (
                self._c.table("session_jobs")
                .insert({"hospital_id": hospital_id, "user_id": user_id, "runner": self._runner})
                .execute().data
            )
        except APIError as exc:
            if exc.code == UNIQUE_VIOLATION:
                raise SessionJobTakenError from exc
            raise
        return _job(rows[0])

    def get_job(self, job_id: str) -> SessionJob | None:
        rows = self._c.table("session_jobs").select(_JOB_COLUMNS).eq("id", job_id).limit(1).execute().data
        return _job(rows[0]) if rows else None

    def jobs(self, hospital_id: int | None, limit: int) -> list[SessionJob]:
        q = self._c.table("session_jobs").select(_JOB_COLUMNS)
        if hospital_id is not None:
            q = q.eq("hospital_id", hospital_id)
        return [_job(r) for r in q.order("created_at", desc=True).limit(limit).execute().data]

    def open_jobs_of_user(self, user_id: str) -> list[SessionJob]:
        rows = (
            self._c.table("session_jobs").select(_JOB_COLUMNS).eq("user_id", user_id).in_("status", list(OPEN))
            .execute().data
        )
        return [_job(r) for r in rows]

    def update_job(self, job_id: str, values: dict) -> None:
        # Only an open job changes: one failed at a restart is never turned into "done" later.
        self._c.table("session_jobs").update(values).eq("id", job_id).in_("status", list(OPEN)).execute()

    def delete_job(self, job_id: str) -> None:
        self._c.table("session_jobs").delete().eq("id", job_id).execute()

    def fail_open_jobs(self, error: str, now: datetime) -> int:
        rows = (
            self._c.table("session_jobs")
            .update({"status": "failed", "error": error, "finished_at": now.isoformat()})
            .in_("status", list(OPEN)).eq("runner", self._runner).execute().data
        )
        return len(rows or [])

    def purge_jobs(self, cutoff: datetime) -> int:
        rows = (
            self._c.table("session_jobs").delete().not_.in_("status", list(OPEN))
            .lt("created_at", cutoff.isoformat()).execute().data
        )
        return len(rows or [])
