"""Supabase-backed draft jobs (table public.draft_jobs) and the in-process worker pool."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from postgrest.exceptions import APIError
from supabase import Client

from irb_watch.services.draft_jobs import OPEN, Job, JobTakenError, JobTargetGoneError

UNIQUE_VIOLATION = "23505"
FOREIGN_KEY_VIOLATION = "23503"
_COLUMNS = "id, user_id, kind, status, error, draft_id, pipeline_step_id, created_at, started_at, finished_at"


def _job(r: dict) -> Job:
    return Job(
        id=str(r["id"]), user_id=str(r["user_id"]), kind=r["kind"], status=r["status"],
        created_at=r["created_at"], error=r.get("error"),
        draft_id=str(r["draft_id"]) if r.get("draft_id") else None,
        pipeline_step_id=r.get("pipeline_step_id"),
        started_at=r.get("started_at"), finished_at=r.get("finished_at"),
    )


class SupabaseJobStore:
    def __init__(self, client: Client, runner: str = "dev") -> None:
        self._c = client
        self._runner = runner  # this server's name (APP_ENV); see fail_open

    def create(self, user_id: str, kind: str, draft_id: str | None, step_id: int | None) -> Job:
        org = (
            self._c.table("organization_members").select("organization_id").eq("user_id", user_id)
            .limit(1).execute().data
        )
        values = {
            "user_id": user_id, "kind": kind, "draft_id": draft_id, "pipeline_step_id": step_id,
            "runner": self._runner,
        }
        if org:
            values["organization_id"] = org[0]["organization_id"]
        try:
            rows = self._c.table("draft_jobs").insert(values).execute().data
        except APIError as exc:
            if exc.code == UNIQUE_VIOLATION:
                raise JobTakenError from exc
            if exc.code == FOREIGN_KEY_VIOLATION:  # the draft or step vanished meanwhile
                raise JobTargetGoneError from exc
            raise
        return _job(rows[0])

    def get(self, job_id: str) -> Job | None:
        rows = self._c.table("draft_jobs").select(_COLUMNS).eq("id", job_id).limit(1).execute().data
        return _job(rows[0]) if rows else None

    def for_user(self, user_id: str, since: datetime) -> list[Job]:
        table = self._c.table("draft_jobs")
        open_rows = (
            table.select(_COLUMNS).eq("user_id", user_id).in_("status", list(OPEN)).execute().data
        )
        other = (
            self._c.table("draft_jobs").select(_COLUMNS).eq("user_id", user_id)
            .not_.in_("status", list(OPEN)).gte("created_at", since.isoformat())
            .order("created_at", desc=True).limit(50).execute().data
        )
        jobs = [_job(r) for r in open_rows + other]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def update(self, job_id: str, values: dict) -> None:
        # Only an open job changes: a job already failed (for example at a restart) is never
        # turned into "done" by a worker that finishes late.
        self._c.table("draft_jobs").update(values).eq("id", job_id).in_("status", list(OPEN)).execute()

    def delete(self, job_id: str) -> None:
        self._c.table("draft_jobs").delete().eq("id", job_id).execute()

    def fail_open(self, error: str, now: datetime) -> int:
        rows = (
            self._c.table("draft_jobs")
            .update({"status": "failed", "error": error, "finished_at": now.isoformat()})
            .in_("status", list(OPEN))
            .eq("runner", self._runner)  # only this server's own jobs
            .execute()
            .data
        )
        return len(rows or [])

    def purge_before(self, cutoff: datetime) -> int:
        rows = (
            self._c.table("draft_jobs").delete().not_.in_("status", list(OPEN))
            .lt("created_at", cutoff.isoformat()).execute().data
        )
        return len(rows or [])


class ThreadRunner:
    """A small pool inside the API process. A separate worker can replace it later: the queue
    is the table, the runner only decides where the work runs."""

    def __init__(self, workers: int = 2) -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="draft-job")

    def submit(self, work: Callable[[], None]) -> None:
        self._pool.submit(work)
