"""ALL-SEARCH: read a hospital's whole backlog, then let the AI look for IRB dates in it.

Owner decisions (2026-11-18; docs/decisions.md "IRB date recommendations for users"):
  * A platform admin runs it by hand, per hospital: for hospitals that had a cap of 200 and then
    50 posts per board, and for every new hospital. Two steps, each a job with a status, a readable
    reason when it fails and a STOP:
      1. READ: every board of the hospital that is its own (R-BAY, public SNUH) is read in batches
         of unstored posts, newest first, at most 500 posts per board (soft: up to one batch more).
         Saved QUIETLY (no cards, summaries or mails, no removals, the source's scan status left
         as it was; see ScanService.run_backlog_batch). Each batch is saved on its own, so a stop,
         a dropped session or a restart loses at most the batch in progress and a new READ
         continues with the posts not stored yet.
      2. AI: the admin sees the post, batch and character counts first and confirms. Candidates:
         posts with a schedule word in the title plus the newest 15 of each notice board, at most
         10 batches of about 60,000 characters per run. A new AI step skips posts an earlier one
         already read, so "run it again" continues.
  * The AI writes NEW proposals only (like the daily search). The daily search's cursor is not
    touched: the backlog's versions are `baseline`, which the daily search never reads as changes.
  * A READ is refused while a scan run is going (same R-BAY login pool) and a scan is refused by
    the source claim while a batch holds it.
  * One open job per hospital; one worker for all ALL-SEARCH jobs (an hour-long read must not
    starve drafts or the ordinary session search); open jobs are failed at start-up.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from irb_watch.crawlers.errors import LoginExpiredError, LoginRejectedError
from irb_watch.services.draft_jobs import OPEN, JobRunner
from irb_watch.services.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceError,
    UnavailableError,
)
from irb_watch.services.scan import BacklogBatch, ScanService
from irb_watch.services.sessions import (
    POST_CHARS,
    SCHEDULE_WORDS,
    TOTAL_CHARS,
    NoReadableTextError,
    PostBrief,
    SessionService,
)
from irb_watch.services.sources import Source
from irb_watch.services.users import AuditSink, UserStore, active_profile

log = logging.getLogger(__name__)

BATCH_POSTS = 50  # posts opened per batch (a batch is one scan-sized unit of work: well under the claim's 30 minutes)
MAX_POSTS_PER_BOARD = 500  # soft: the last batch may pass it by less than BATCH_POSTS
NEWEST_NOTICES = 15  # newest posts of each notice board the AI reads besides the schedule titles
MAX_CANDIDATES = 150
MAX_AI_BATCHES = 10
RESTARTED = "the server restarted while the search was running; start it again (what was stored is kept)"
STOP_CHECK_SECONDS = 2.0
JOB_RETENTION_DAYS = 30


@dataclass(frozen=True)
class SearchJob:
    id: str
    hospital_id: int
    user_id: str
    stage: str  # read | ai
    status: str  # queued | running | done | stopped | failed
    created_at: str
    stop_requested: bool = False
    error: str | None = None
    progress: dict = field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def open(self) -> bool:
        return self.status in OPEN


@dataclass(frozen=True)
class Candidate:
    """A stored post the AI step may read."""

    post: PostBrief
    board_kind: str


class JobTakenError(Exception):
    """Raised by the store when the hospital already has an open job (unique index)."""


class AllSearchStore(Protocol):
    def boards(self, hospital_id: int) -> list[Source]:
        """The hospital's active boards that are its own (R-BAY, public SNUH), manual and study
        sources left out, by id."""
        ...

    def candidates(self, hospital_id: int) -> list[Candidate]:
        """Live stored posts of the boards above that have AI on, newest first."""
        ...

    def chars_of(self, version_ids: list[int]) -> dict[int, int]:
        """The text of each version (page text plus file texts) in characters, each capped at POST_CHARS."""
        ...

    def create_job(self, hospital_id: int, user_id: str, stage: str) -> SearchJob:
        """JobTakenError when the hospital has an open job."""
        ...

    def get_job(self, job_id: str) -> SearchJob | None: ...
    def jobs(self, hospital_id: int, limit: int) -> list[SearchJob]: ...
    def update_job(self, job_id: str, values: dict) -> None:
        """Changes an OPEN job only."""
        ...

    def request_stop(self, job_id: str) -> bool:
        """Sets the stop flag of an open job; False when it is not open."""
        ...

    def fail_open_jobs(self, error: str, now: datetime) -> int: ...
    def purge_jobs(self, cutoff: datetime) -> int: ...


def is_schedule_title(title: str) -> bool:
    low = title.lower()
    return any(w in low for w in SCHEDULE_WORDS)


def pick_candidates(candidates: list[Candidate], skip_versions: set[int]) -> list[Candidate]:
    """Schedule-word titles (all of them, newest first) plus the newest NEWEST_NOTICES of each notice
    board, minus posts an earlier AI step read, at most MAX_CANDIDATES (schedule titles first)."""
    fresh = [c for c in candidates if c.post.version_id not in skip_versions]
    scheduled = [c for c in fresh if is_schedule_title(c.post.title)]
    taken = {c.post.document_id for c in scheduled}
    notices = [c for c in fresh if c.board_kind == "notice" and c.post.document_id not in taken]
    return (scheduled + notices[:NEWEST_NOTICES])[:MAX_CANDIDATES]


def make_batches(posts: list[PostBrief], chars: dict[int, int]) -> list[list[PostBrief]]:
    """Posts in order, grouped so each group's text fits one AI call (TOTAL_CHARS). A post with no
    text counts as nothing and goes with its neighbours."""
    batches: list[list[PostBrief]] = []
    current: list[PostBrief] = []
    size = 0
    for p in posts:
        n = min(chars.get(p.version_id, 0), POST_CHARS)
        if current and size + n > TOTAL_CHARS:
            batches.append(current)
            current, size = [], 0
        current.append(p)
        size += n
    if current:
        batches.append(current)
    return batches


@dataclass(frozen=True)
class Preview:
    stored_posts: int  # live stored posts of the hospital's own boards with AI on
    posts: int  # candidates the AI would read
    chars: int
    batches: int  # all batches the candidates need
    run_batches: int  # batches this run would send (at most MAX_AI_BATCHES)
    left_over: int  # posts that stay for another run
    enabled: bool  # the AI is set up on this server


class AllSearchService:
    def __init__(
        self,
        users: UserStore,
        store: AllSearchStore,
        scans: ScanService,
        sessions: SessionService,
        audit: AuditSink,
        runner: JobRunner,
        *,
        scan_running: Callable[[], bool] = lambda: False,
        hospital_exists: Callable[[int], bool] = lambda _id: True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._users = users
        self._store = store
        self._scans = scans
        self._sessions = sessions
        self._audit = audit
        self._runner = runner
        self._scan_running = scan_running
        self._hospital_exists = hospital_exists
        self._clock = clock

    def _admin(self, user_id: str) -> None:
        if active_profile(self._users, user_id, "your account is not active").role != "admin":
            raise ForbiddenError("admin only")

    def _now(self) -> str:
        return self._clock().isoformat()

    def _hospital(self, hospital_id: int) -> None:
        if not self._hospital_exists(hospital_id):
            raise NotFoundError("hospital not found")

    # -- reading -------------------------------------------------------------------------------

    def jobs(self, hospital_id: int, limit: int = 10) -> list[SearchJob]:
        self._hospital(hospital_id)
        return self._store.jobs(hospital_id, limit)

    def job(self, job_id: str) -> SearchJob:
        job = self._store.get_job(job_id)
        if job is None:
            raise NotFoundError("job not found")
        return job

    def stop(self, admin_id: str, job_id: str) -> SearchJob:
        """Ask a job to stop: a READ stops before its next post (what it read is saved), the AI
        step before its next batch."""
        self._admin(admin_id)
        job = self.job(job_id)
        if not self._store.request_stop(job_id):
            raise ConflictError("this job is not running any more")
        self._audit.record(
            "admin", "session.all_search_stop", admin_id, title="ALL-SEARCH stop requested",
            payload={"hospital_id": job.hospital_id, "job_id": job_id, "stage": job.stage},
        )
        return self.job(job_id)

    # -- step 1: read the backlog ----------------------------------------------------------------

    def start_read(self, admin_id: str, hospital_id: int) -> SearchJob:
        self._admin(admin_id)
        self._hospital(hospital_id)
        boards = self._store.boards(hospital_id)
        if not boards:
            raise ConflictError("this hospital has no active board of its own to read")
        unscanned = [b.name for b in boards if b.last_scan_at is None]
        if unscanned:
            raise ConflictError("scan these boards once first (their first scan is the silent baseline): " + ", ".join(unscanned))
        if self._scan_running():
            raise ConflictError("a scan run is going on; try again when it has finished")
        job = self._create(hospital_id, admin_id, "read")
        self._audit.record(  # counted before the work starts (usage per admin and action)
            "admin", "session.all_search_read_started", admin_id, title="ALL-SEARCH read started",
            payload={"hospital_id": hospital_id, "job_id": job.id, "boards": len(boards)},
        )
        self._submit(job, lambda: self._run_read(job, boards))
        return self._store.get_job(job.id) or job

    def _create(self, hospital_id: int, admin_id: str, stage: str) -> SearchJob:
        try:
            return self._store.create_job(hospital_id, admin_id, stage)
        except JobTakenError as exc:
            raise ConflictError("an ALL-SEARCH job of this hospital is running; stop it or wait") from exc

    def _submit(self, job: SearchJob, work: Callable[[], None]) -> None:
        try:
            self._runner.submit(work)
        except Exception as exc:
            log.exception("all-search job %s could not be queued", job.id)
            self._finish(job.id, "failed", error="the job could not be started; please try again")
            raise ServiceError("the job could not be started; please try again") from exc

    def _finish(self, job_id: str, status: str, **values) -> None:
        try:
            self._store.update_job(job_id, {"status": status, "finished_at": self._now(), **values})
        except Exception:
            log.exception("all-search job %s could not be marked %s", job_id, status)

    def _stop_check(self, job_id: str) -> Callable[[], bool]:
        """Whether a stop was asked: the database is looked at most every STOP_CHECK_SECONDS."""
        state = {"at": 0.0, "stop": False}

        def check() -> bool:
            if state["stop"]:
                return True
            now = time.monotonic()
            if now - state["at"] >= STOP_CHECK_SECONDS:
                state["at"] = now
                try:
                    job = self._store.get_job(job_id)
                    state["stop"] = bool(job and job.stop_requested)
                except Exception:
                    log.exception("the stop flag of job %s could not be read", job_id)
            return bool(state["stop"])

        return check

    def _run_read(self, job: SearchJob, boards: list[Source]) -> None:
        """Never raises (a worker thread). Ends in done, stopped or failed with a reason."""
        try:
            self._store.update_job(job.id, {"status": "running", "started_at": self._now()})
        except Exception:
            log.exception("all-search job %s could not be marked running", job.id)
        progress: dict = {"boards": {str(b.id): {"name": b.name, "stored": 0, "total": 0, "left": None,
                                                 "failed": 0, "flagged": 0, "state": "waiting", "error": None}
                                    for b in boards}}
        errors: list[str] = []
        stopped = False
        try:
            should_stop = self._stop_check(job.id)
            for board in boards:
                if should_stop():
                    stopped = True
                    break
                result = self._read_board(job, board, progress, should_stop)
                if result == "stopped":
                    stopped = True
                    break
                if result.startswith("error:"):
                    errors.append(f"{board.name}: {result[6:]}")
                    if result.startswith("error:!"):  # the login or the session: the other boards would fail the same way
                        break
        except Exception as exc:
            log.exception("all-search read job %s crashed", job.id)
            errors.append(f"unexpected error ({type(exc).__name__}); see the server log")
        status = "failed" if errors else ("stopped" if stopped else "done")
        self._finish(job.id, status, progress=progress, error="; ".join(errors)[:1000] or None)
        try:
            self._audit.record(
                "admin", "session.all_search_read_finished", job.user_id, title="ALL-SEARCH read finished",
                payload={
                    "hospital_id": job.hospital_id, "job_id": job.id, "status": status,
                    "stored": sum(b["stored"] for b in progress["boards"].values()),
                },
            )
        except Exception:
            log.exception("the end of all-search job %s could not be logged", job.id)

    def _save_progress(self, job_id: str, progress: dict) -> None:
        try:
            self._store.update_job(job_id, {"progress": progress})
        except Exception:
            log.exception("the progress of all-search job %s could not be saved", job_id)

    def _read_board(self, job: SearchJob, board: Source, progress: dict, should_stop: Callable[[], bool]) -> str:
        """Batches until nothing is left, the cap is reached, a stop is asked or a batch fails.
        Returns 'done', 'stopped' or 'error:<reason>' (an 'error:!' reason ends the whole job)."""
        entry = progress["boards"][str(board.id)]
        entry["state"] = "reading"
        skip: set[str] = set()  # posts that failed in this run: not tried again
        while True:
            if should_stop():
                entry["state"] = "stopped"
                self._save_progress(job.id, progress)
                return "stopped"
            if self._scan_running():
                entry["state"], entry["error"] = "paused", "a scan run started; start ALL-SEARCH again later"
                self._save_progress(job.id, progress)
                return "error:a scan run started; start ALL-SEARCH again later"
            try:
                source = self._scans.begin_backlog(board.id)
                batch: BacklogBatch = self._scans.run_backlog_batch(source, frozenset(skip), BATCH_POSTS, should_stop)
            except (LoginRejectedError, LoginExpiredError) as exc:
                entry["state"], entry["error"] = "failed", str(exc)[:300]
                self._save_progress(job.id, progress)
                return f"error:!{str(exc)[:300]}"
            except ServiceError as exc:  # refused (claimed by a scan, inactive, ...): the reason is shown
                entry["state"], entry["error"] = "failed", exc.message[:300]
                self._save_progress(job.id, progress)
                return f"error:{exc.message[:300]}"
            except Exception as exc:  # noqa: BLE001 - a crawler or database error: its text is the reason
                reason = (str(exc).splitlines() or [type(exc).__name__])[0][:300]
                entry["state"], entry["error"] = "failed", reason
                self._save_progress(job.id, progress)
                return f"error:{reason}"
            skip |= set(batch.failed)
            entry["stored"] += batch.stored
            entry["total"] = batch.total
            entry["left"] = batch.left
            entry["failed"] += len(batch.failed)
            entry["flagged"] += batch.flagged
            self._save_progress(job.id, progress)
            if batch.stopped:
                entry["state"] = "stopped"
                self._save_progress(job.id, progress)
                return "stopped"
            if batch.stored == 0 or batch.left == 0:
                entry["state"] = "done"
                self._save_progress(job.id, progress)
                return "done"
            if batch.total >= MAX_POSTS_PER_BOARD:
                entry["state"] = "cap"  # the cap was reached: the newest ones are stored
                self._save_progress(job.id, progress)
                return "done"

    # -- step 2: the AI over what is stored ---------------------------------------------------------

    def _read_versions(self, hospital_id: int) -> set[int]:
        out: set[int] = set()
        for j in self._store.jobs(hospital_id, 50):
            if j.stage == "ai" and not j.open:
                out |= set(j.progress.get("read_versions") or [])
        return out

    def _plan(self, hospital_id: int) -> tuple[int, list[Candidate], list[list[PostBrief]], dict[int, int]]:
        """(stored posts, the candidates, their batches, their sizes): one read of the posts."""
        stored = self._store.candidates(hospital_id)
        picked = pick_candidates(stored, self._read_versions(hospital_id))
        chars = self._store.chars_of([c.post.version_id for c in picked]) if picked else {}
        return len(stored), picked, make_batches([c.post for c in picked], chars), chars

    def preview(self, admin_id: str, hospital_id: int) -> Preview:
        """What the AI step would read and how many calls it needs, before anything is spent."""
        self._admin(admin_id)
        self._hospital(hospital_id)
        stored, picked, batches, chars = self._plan(hospital_id)
        run = batches[:MAX_AI_BATCHES]
        return Preview(
            stored_posts=stored,
            posts=len(picked),
            chars=sum(min(chars.get(c.post.version_id, 0), POST_CHARS) for c in picked),
            batches=len(batches),
            run_batches=len(run),
            left_over=sum(len(b) for b in batches[MAX_AI_BATCHES:]),
            enabled=self._sessions.finder_enabled,
        )

    def start_ai(self, admin_id: str, hospital_id: int) -> SearchJob:
        self._admin(admin_id)
        self._hospital(hospital_id)
        if not self._sessions.finder_enabled:
            raise UnavailableError("finding sessions with AI is not set up on this server (OPENAI_API_KEY, OPENAI_MODEL)")
        _, _, batches, _ = self._plan(hospital_id)
        if not batches:
            raise ConflictError("there are no stored posts left for the AI to read; read the backlog first, or everything was read")
        run = batches[:MAX_AI_BATCHES]
        job = self._create(hospital_id, admin_id, "ai")
        self._audit.record(  # counted before the model is called
            "admin", "session.all_search_ai_started", admin_id, title="ALL-SEARCH AI step started",
            payload={"hospital_id": hospital_id, "job_id": job.id, "batches": len(run), "posts": sum(len(b) for b in run)},
        )
        self._submit(job, lambda: self._run_ai(job, run))
        return self._store.get_job(job.id) or job

    def _run_ai(self, job: SearchJob, batches: list[list[PostBrief]]) -> None:
        """Never raises (a worker thread)."""
        try:
            self._store.update_job(job.id, {"status": "running", "started_at": self._now()})
        except Exception:
            log.exception("all-search job %s could not be marked running", job.id)
        progress: dict = {
            "batches": len(batches), "batches_done": 0, "posts": sum(len(b) for b in batches),
            "proposed": 0, "skipped": 0, "read_versions": [],
        }
        error: str | None = None
        stopped = False
        should_stop = self._stop_check(job.id)
        try:
            for batch in batches:
                if should_stop():
                    stopped = True
                    break
                try:
                    result = self._sessions.propose_from_posts(job.hospital_id, batch, job.user_id, None)
                except NoReadableTextError:
                    result = {"documents_read": 0, "proposed": 0, "skipped": 0}
                progress["batches_done"] += 1
                progress["proposed"] += result.get("proposed", 0)
                progress["skipped"] += result.get("skipped", 0)
                progress["read_versions"] += [p.version_id for p in batch]
                self._save_progress(job.id, progress)
        except ServiceError as exc:
            error = exc.message[:1000]
        except Exception as exc:
            log.exception("all-search AI job %s failed", job.id)
            error = f"the AI service or the database failed ({type(exc).__name__}); try again later"
        self._finish(job.id, "failed" if error else ("stopped" if stopped else "done"), progress=progress, error=error)
        try:
            self._audit.record(
                "admin", "session.all_search_ai_finished", job.user_id, title="ALL-SEARCH AI step finished",
                payload={
                    "hospital_id": job.hospital_id, "job_id": job.id, "ok": error is None and not stopped,
                    "batches_done": progress["batches_done"], "proposed": progress["proposed"],
                },
            )
        except Exception:
            log.exception("the end of all-search job %s could not be logged", job.id)

    # -- housekeeping ------------------------------------------------------------------------------

    def fail_unfinished(self) -> int:
        try:
            count = self._store.fail_open_jobs(RESTARTED, self._clock())
        except Exception:
            log.exception("open all-search jobs could not be marked failed at start")
            return 0
        if count:
            log.warning("%d all-search job(s) were open at start and are marked failed", count)
        return count

    def purge_old(self) -> int:
        try:
            return self._store.purge_jobs(self._clock() - timedelta(days=JOB_RETENTION_DAYS))
        except Exception:
            log.exception("old all-search jobs could not be deleted")
            return 0

