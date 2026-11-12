"""Draft jobs: every AI draft and AI revision runs in the background with a status.

Rules (owner, 2026-11-12; see docs/decisions.md "Draft jobs"):
  * Starting a job first checks the limits (at most MAX_OPEN_PER_USER open jobs per user, one
    per pipeline step, one open revision per draft), then makes the job row, then runs the
    feature's own checks (access, daily limit, the audit entry that counts the call). A refusal
    there is answered at once, as before, and leaves no job behind.
  * The work (the model call and saving the result) then runs in the API process. Its inputs
    are held in memory, so a job that was open when the server stopped cannot continue: at the
    next start it is marked failed with a reason and is never run again by itself. The attempt
    stays counted.
  * A failure always has a reason the user can read; the job is never left "running".
  * Users see their jobs on the page only (no mail). Finished jobs are deleted after 30 days.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from irb_watch.services.errors import (
    ConflictError,
    NotFoundError,
    ServiceError,
    TooManyRequestsError,
)

log = logging.getLogger(__name__)

MAX_OPEN_PER_USER = 2
OPEN = ("queued", "running")
RESTARTED = "the server restarted while this was being written; start it again"
RETENTION_DAYS = 30
SHOW_FAILED_HOURS = 24


@dataclass(frozen=True)
class Job:
    id: str
    user_id: str
    kind: str  # draft | revision
    status: str  # queued | running | done | failed
    created_at: str
    error: str | None = None
    draft_id: str | None = None
    pipeline_step_id: int | None = None
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def open(self) -> bool:
        return self.status in OPEN


class JobTakenError(Exception):
    """Raised by the store when the step or draft already has an open job (unique index)."""


class JobTargetGoneError(Exception):
    """Raised by the store when the job's draft or step no longer exists (foreign key)."""


class JobStore(Protocol):
    def create(self, user_id: str, kind: str, draft_id: str | None, step_id: int | None) -> Job: ...
    def get(self, job_id: str) -> Job | None: ...
    def for_user(self, user_id: str, since: datetime) -> list[Job]:
        """The user's open jobs, and the others made since `since`; newest first."""
        ...

    def update(self, job_id: str, values: dict) -> None:
        """Changes an OPEN job only: a job that already ended keeps its outcome."""
        ...

    def delete(self, job_id: str) -> None: ...
    def fail_open(self, error: str, now: datetime) -> int:
        """Fails the open jobs THIS server made (not another server's on the same database)."""
        ...

    def purge_before(self, cutoff: datetime) -> int: ...


class JobRunner(Protocol):
    def submit(self, work: Callable[[], None]) -> None: ...


class InlineRunner:
    """Runs the work at once in the caller (tests, and a server without a worker pool)."""

    def submit(self, work: Callable[[], None]) -> None:
        work()


def _draft_id_of(result: object) -> str | None:
    """A new draft's id from the feature's result (DraftDetail), if it has one."""
    draft = getattr(result, "draft", None)
    return getattr(draft, "id", None)


class DraftJobService:
    def __init__(
        self,
        store: JobStore,
        runner: JobRunner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._runner = runner
        self._clock = clock

    # -- starting ------------------------------------------------------------------------------

    def start(
        self,
        user_id: str,
        kind: str,
        prepare: Callable[[], Callable[[], object]],
        draft_id: str | None = None,
        step_id: int | None = None,
        owns: Callable[[], object] | None = None,
    ) -> Job:
        """prepare: the feature's checks; it returns the work to run in the background. Raising
        there refuses the request and removes the job again.
        owns: run before anything is written; it raises (NotFoundError) unless the draft or
        step is the user's own. Without it a stranger's id would hold the step's or draft's
        one open job for a moment and tell them whether the id exists."""
        if owns is not None:
            owns()
        open_jobs = [j for j in self._store.for_user(user_id, self._clock()) if j.open]
        if len(open_jobs) >= MAX_OPEN_PER_USER:
            raise TooManyRequestsError(
                f"{MAX_OPEN_PER_USER} of your drafts are being written already; wait for one to finish"
            )
        if step_id is not None and any(j.pipeline_step_id == step_id for j in open_jobs):
            raise ConflictError("a draft for this step is being written already")
        if kind == "revision" and any(j.kind == "revision" and j.draft_id == draft_id for j in open_jobs):
            raise ConflictError("this draft is being revised already; wait for it to finish")
        try:
            job = self._store.create(user_id, kind, draft_id, step_id)
        except JobTakenError as exc:
            raise ConflictError("this is being written already; wait for it to finish") from exc
        except JobTargetGoneError as exc:
            raise NotFoundError("the draft or step no longer exists; reload the page") from exc
        # Two requests at the same moment may both have passed the check above: the newest
        # jobs beyond the limit give way.
        still_open = sorted(
            (j for j in self._store.for_user(user_id, self._clock()) if j.open),
            key=lambda j: (j.created_at, j.id),
        )
        if job.id not in {j.id for j in still_open[:MAX_OPEN_PER_USER]}:
            self._discard(job.id)
            raise TooManyRequestsError(
                f"{MAX_OPEN_PER_USER} of your drafts are being written already; wait for one to finish"
            )
        try:
            work = prepare()
        except BaseException:
            self._discard(job.id)
            raise
        try:
            self._runner.submit(lambda: self._run(job.id, work))
        except Exception as exc:
            log.exception("draft job %s could not be queued", job.id)
            self._finish(job.id, "failed", error="the draft could not be started; please try again")
            raise ServiceError("the draft could not be started; please try again") from exc
        return self._store.get(job.id) or job

    def _discard(self, job_id: str) -> None:
        try:
            self._store.delete(job_id)
        except Exception:
            log.exception("refused draft job %s could not be removed", job_id)
            self._finish(job_id, "failed", error="refused before it started")

    # -- running -------------------------------------------------------------------------------

    def _run(self, job_id: str, work: Callable[[], object]) -> None:
        """Never raises: it runs in a worker thread. Every outcome ends in done or failed."""
        try:
            self._store.update(job_id, {"status": "running", "started_at": self._clock().isoformat()})
        except Exception:
            log.exception("draft job %s could not be marked running", job_id)
        try:
            result = work()
        except ServiceError as exc:
            # Written for users already (model failure, limit, a draft that changed meanwhile).
            self._finish(job_id, "failed", error=exc.message)
            return
        except Exception as exc:
            log.exception("draft job %s failed", job_id)
            self._finish(
                job_id, "failed", error=f"unexpected error ({type(exc).__name__}); please try again"
            )
            return
        self._finish(job_id, "done", draft_id=_draft_id_of(result))

    def _finish(self, job_id: str, status: str, error: str | None = None, draft_id: str | None = None) -> None:
        values: dict = {"status": status, "finished_at": self._clock().isoformat()}
        if error is not None:
            values["error"] = error[:1000]
        if draft_id is not None:
            values["draft_id"] = draft_id
        try:
            self._store.update(job_id, values)
        except Exception:
            # The job then stays open until the next start marks it failed (RESTARTED).
            log.exception("draft job %s could not be marked %s", job_id, status)

    # -- reading -------------------------------------------------------------------------------

    def get(self, user_id: str, job_id: str) -> Job:
        job = self._store.get(job_id)
        if job is None or job.user_id != user_id:
            raise NotFoundError("job not found")
        return job

    def list(self, user_id: str) -> list[Job]:
        """Open jobs, and jobs that failed in the last 24 hours (so the reason can be read)."""
        since = self._clock() - timedelta(hours=SHOW_FAILED_HOURS)
        return [j for j in self._store.for_user(user_id, since) if j.open or j.status == "failed"]

    # -- housekeeping --------------------------------------------------------------------------

    def fail_unfinished(self) -> int:
        """At server start: jobs left open by the last run cannot continue."""
        try:
            count = self._store.fail_open(RESTARTED, self._clock())
        except Exception:
            log.exception("open draft jobs could not be marked failed at start")
            return 0
        if count:
            log.warning("%d draft job(s) were open at start and are marked failed", count)
        return count

    def purge_old(self, days: int = RETENTION_DAYS) -> int:
        """Never raises: it runs from the scheduler."""
        try:
            return self._store.purge_before(self._clock() - timedelta(days=days))
        except Exception:
            log.exception("old draft jobs could not be deleted")
            return 0
