"""ALL-SEARCH (services/session_all.py): the backlog read and the AI step, with in-memory stand-ins."""

import itertools
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from irb_watch.crawlers.errors import LoginRejectedError
from irb_watch.services import session_all
from irb_watch.services.draft_jobs import OPEN
from irb_watch.services.errors import ConflictError, ForbiddenError, ServiceError, UnavailableError
from irb_watch.services.scan import BacklogBatch
from irb_watch.services.session_all import (
    AllSearchService,
    Candidate,
    JobTakenError,
    SearchJob,
    make_batches,
    pick_candidates,
)
from irb_watch.services.sessions import PostBrief
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_scan import make_source

NOW = datetime(2026, 11, 20, 1, tzinfo=UTC)
H = 7


class FakeStoreAS:
    def __init__(self) -> None:
        self.board_rows = [
            make_source(1, name="병원 : 공지", hospital_id=H, board_kind="notice", last_scan_at="2026-11-19T00:00:00+00:00"),
            make_source(2, name="병원 : 양식함", hospital_id=H, board_kind="forms", last_scan_at="2026-11-19T00:00:00+00:00"),
        ]
        self.cands: list[Candidate] = []
        self.chars: dict[int, int] = {}
        self.job_rows: dict[str, SearchJob] = {}

    def boards(self, hospital_id):
        return list(self.board_rows)

    def candidates(self, hospital_id):
        return list(self.cands)

    def chars_of(self, version_ids):
        return {v: self.chars.get(v, 1000) for v in version_ids}

    def create_job(self, hospital_id, user_id, stage):
        if any(j.hospital_id == hospital_id and j.status in OPEN for j in self.job_rows.values()):
            raise JobTakenError
        job = SearchJob(str(uuid.uuid4()), hospital_id, user_id, stage, "queued", NOW.isoformat())
        self.job_rows[job.id] = job
        return job

    def get_job(self, job_id):
        return self.job_rows.get(job_id)

    def jobs(self, hospital_id, limit):
        return [j for j in reversed(list(self.job_rows.values())) if j.hospital_id == hospital_id][:limit]

    def update_job(self, job_id, values):
        job = self.job_rows[job_id]
        if job.status in OPEN:
            self.job_rows[job_id] = replace(job, **values)

    def request_stop(self, job_id):
        job = self.job_rows.get(job_id)
        if job is None or job.status not in OPEN:
            return False
        self.job_rows[job_id] = replace(job, stop_requested=True)
        return True

    def fail_open_jobs(self, error, now):
        n = 0
        for i, j in list(self.job_rows.items()):
            if j.status in OPEN:
                self.job_rows[i] = replace(j, status="failed", error=error)
                n += 1
        return n

    def purge_jobs(self, cutoff):
        return 0


class FakeScans:
    """begin_backlog / run_backlog_batch: `plan[source id]` is the list of batches (or errors) to return."""

    def __init__(self) -> None:
        self.plan: dict[int, list] = {}
        self.calls: list[tuple[int, frozenset, int]] = []
        self.after_batch = None  # called after each batch (a test hook)

    def begin_backlog(self, source_id):
        return next(s for s in FakeStoreAS().board_rows if s.id == source_id)

    def run_backlog_batch(self, source, skip_ids, limit, should_stop):
        self.calls.append((source.id, frozenset(skip_ids), limit))
        step = self.plan[source.id].pop(0)
        if isinstance(step, Exception):
            raise step
        if self.after_batch:
            self.after_batch()
        return step


class FakeSessionsAI:
    def __init__(self, enabled=True) -> None:
        self.finder_enabled = enabled
        self.calls: list[tuple[int, list[int], str, object]] = []
        self.error: Exception | None = None
        self.proposed = 1

    def propose_from_posts(self, hospital_id, posts, proposer_id, job_id):
        self.calls.append((hospital_id, [p.version_id for p in posts], proposer_id, job_id))
        if self.error:
            raise self.error
        return {"documents_read": len(posts), "proposed": self.proposed, "skipped": 0}


class InlineRunner:
    def submit(self, work):
        work()


class Env:
    def __init__(self, ai=True, scan_running=False) -> None:
        self.users = FakeStore()
        self.users.add("boss", "boss@example.com", role="admin")
        self.users.add("user", "user@example.com")
        self.store, self.scans, self.sessions = FakeStoreAS(), FakeScans(), FakeSessionsAI(ai)
        self.audit = FakeAudit()
        self.running = scan_running
        self.service = AllSearchService(
            self.users, self.store, self.scans, self.sessions, self.audit, InlineRunner(),
            scan_running=lambda: self.running, hospital_exists=lambda h: h == H, clock=lambda: NOW,
        )

    def last(self) -> SearchJob:
        return list(self.store.job_rows.values())[-1]


@pytest.fixture(autouse=True)
def _no_stop_throttle(monkeypatch):
    monkeypatch.setattr(session_all, "STOP_CHECK_SECONDS", 0)


@pytest.fixture
def env():
    return Env()


def batch(stored, left, total, **kw):
    return BacklogBatch(stored=stored, failed=kw.get("failed", {}), left=left, stopped=False, flagged=kw.get("flagged", 0), total=total)


# -- step 1: the read ---------------------------------------------------------------------------


def test_a_read_goes_batch_by_batch_per_board_and_reports_progress(env):
    env.scans.plan = {1: [batch(50, 70, 60), batch(50, 20, 110), batch(20, 0, 130, flagged=3)], 2: [batch(5, 0, 5)]}
    job = env.service.start_read("boss", H)
    done = env.store.get_job(job.id)
    assert (done.status, done.error) == ("done", None)
    first, second = done.progress["boards"]["1"], done.progress["boards"]["2"]
    assert (first["stored"], first["total"], first["left"], first["flagged"], first["state"]) == (120, 130, 0, 3, "done")
    assert (second["stored"], second["state"]) == (5, "done")
    assert [c[2] for c in env.scans.calls] == [50, 50, 50, 50]  # BATCH_POSTS each time
    events = [e["event_type"] for e in env.audit.entries]
    assert events == ["session.all_search_read_started", "session.all_search_read_finished"]


def test_the_cap_per_board_stops_the_reading_and_says_so(env):
    env.scans.plan = {1: [batch(50, 500, 520)], 2: [batch(0, 0, 0)]}
    env.service.start_read("boss", H)
    entry = env.last().progress["boards"]["1"]
    assert (entry["state"], entry["total"]) == ("cap", 520) and len(env.scans.calls) == 2  # 1 batch + the next board


def test_failed_posts_are_not_tried_again_in_this_run_and_a_batch_that_stores_nothing_ends_the_board(env):
    env.scans.plan = {1: [batch(10, 40, 20, failed={"5": "layout changed"}), batch(0, 1, 20)], 2: [batch(0, 0, 0)]}
    env.service.start_read("boss", H)
    assert env.scans.calls[1][1] == frozenset({"5"})  # the second batch skips the post that failed
    entry = env.last().progress["boards"]["1"]
    assert (entry["failed"], entry["state"]) == (1, "done")


def test_a_board_error_shows_its_reason_and_the_other_boards_still_run(env):
    env.scans.plan = {1: [ConflictError("a scan of this source is already running")], 2: [batch(3, 0, 3)]}
    env.service.start_read("boss", H)
    job = env.last()
    assert job.status == "failed" and "병원 : 공지: a scan of this source is already running" in job.error
    assert (job.progress["boards"]["1"]["state"], job.progress["boards"]["2"]["stored"]) == ("failed", 3)


def test_a_rejected_login_ends_the_whole_job(env):
    env.scans.plan = {1: [LoginRejectedError("wrong password")], 2: [batch(1, 0, 1)]}
    env.service.start_read("boss", H)
    job = env.last()
    assert job.status == "failed" and "wrong password" in job.error
    assert job.progress["boards"]["2"]["state"] == "waiting" and len(env.scans.calls) == 1


def test_a_crash_inside_a_batch_is_a_readable_reason_not_a_silent_stop(env):
    env.scans.plan = {1: [RuntimeError("storage upload failed\nstack")], 2: [batch(0, 0, 0)]}
    env.service.start_read("boss", H)
    assert "storage upload failed" in env.last().error and "stack" not in env.last().error


def test_stopping_keeps_what_was_read_and_ends_as_stopped(env):
    env.scans.plan = {1: [batch(50, 100, 50), batch(50, 50, 100)], 2: [batch(1, 0, 1)]}
    env.scans.after_batch = lambda: env.store.request_stop(env.last().id)
    env.service.start_read("boss", H)
    job = env.last()
    assert job.status == "stopped" and job.progress["boards"]["1"]["stored"] == 50
    assert len(env.scans.calls) == 1 and job.progress["boards"]["2"]["state"] == "waiting"


def test_a_read_is_refused_while_a_scan_runs_or_a_board_was_never_scanned_or_a_job_is_open(env):
    env.running = True
    with pytest.raises(ConflictError, match="scan run is going on"):
        env.service.start_read("boss", H)
    env.running = False
    env.store.board_rows[1] = replace(env.store.board_rows[1], last_scan_at=None)
    with pytest.raises(ConflictError, match="scan these boards once first.*양식함"):
        env.service.start_read("boss", H)
    env.store.board_rows[1] = replace(env.store.board_rows[1], last_scan_at="2026-11-19T00:00:00+00:00")
    env.store.create_job(H, "boss", "read")  # one is open already
    with pytest.raises(ConflictError, match="is running"):
        env.service.start_read("boss", H)
    env.store.board_rows = []
    env.store.job_rows.clear()
    with pytest.raises(ConflictError, match="no active board"):
        env.service.start_read("boss", H)


def test_a_scan_that_starts_during_the_read_pauses_it_with_a_reason(env):
    env.scans.plan = {1: [batch(50, 100, 50)], 2: []}
    env.scans.after_batch = lambda: setattr(env, "running", True)
    env.service.start_read("boss", H)
    job = env.last()
    assert job.status == "failed" and "scan run started" in job.error


def test_only_admins_start_stop_or_preview(env):
    for call in (
        lambda: env.service.start_read("user", H), lambda: env.service.start_ai("user", H),
        lambda: env.service.preview("user", H), lambda: env.service.stop("user", "x"),
    ):
        with pytest.raises(ForbiddenError):
            call()


# -- step 2: the AI -------------------------------------------------------------------------------

_ids = itertools.count(1)


def cand(title, kind="notice", posted="2026-11-01"):
    n = next(_ids)
    return Candidate(PostBrief(n, n * 10, title, posted), kind)


def test_candidates_are_schedule_titles_plus_the_newest_notices_minus_what_was_read():
    old_schedule = cand("2025년 IRB 심의 일정", "regulations", "2025-01-01")
    notices = [cand(f"공지 {i}") for i in range(20)]
    forms = cand("양식 변경", "forms")
    got = pick_candidates([*notices, forms, old_schedule], skip_versions=set())
    assert got[0].post.title == "2025년 IRB 심의 일정"  # schedule titles first, whatever the board
    assert [c.post.title for c in got[1:]] == [f"공지 {i}" for i in range(15)]  # newest 15 notices
    assert forms not in got
    read = {old_schedule.post.version_id, notices[0].post.version_id}
    again = pick_candidates([*notices, forms, old_schedule], skip_versions=read)
    assert old_schedule not in again and notices[0] not in again and len(again) == 15


def test_posts_are_grouped_so_each_call_fits_the_text_limit():
    posts = [PostBrief(i, i, f"t{i}", None) for i in range(1, 8)]
    chars = {i: 8000 for i in range(1, 8)}  # 8000 each (POST_CHARS), 60000 per call: 7 posts = 56000
    assert [len(b) for b in make_batches(posts, chars)] == [7]
    chars = {i: 20000 if i < 4 else 8000 for i in range(1, 8)}  # capped at 8000 each: still 7
    assert [len(b) for b in make_batches(posts, chars)] == [7]
    big = [PostBrief(i, i, "t", None) for i in range(1, 10)]
    assert [len(b) for b in make_batches(big, {i: 8000 for i in range(1, 10)})] == [7, 2]


def test_the_preview_counts_before_anything_is_spent_and_the_run_is_limited_to_10_calls(env):
    env.store.cands = [cand(f"심의 일정 {i}") for i in range(100)]  # all schedule titles, 100 posts
    env.store.chars = {c.post.version_id: 8000 for c in env.store.cands}
    p = env.service.preview("boss", H)
    assert (p.stored_posts, p.posts, p.batches, p.run_batches) == (100, 100, 15, 10)
    assert p.left_over == 30  # 10 calls of 7 posts are sent; 30 posts stay for another run
    assert p.chars == 800_000 and p.enabled
    assert env.sessions.calls == [] and env.store.job_rows == {}  # nothing was spent or started


def test_the_ai_step_writes_proposals_and_the_next_run_continues_after_it(env):
    env.store.cands = [cand(f"심의 일정 {i}") for i in range(10)]
    env.store.chars = {c.post.version_id: 8000 for c in env.store.cands}  # 7 posts per call
    env.service.start_ai("boss", H)
    done = env.last()
    assert done.status == "done" and done.progress["batches_done"] == 2 and done.progress["proposed"] == 2
    assert [len(c[1]) for c in env.sessions.calls] == [7, 3] and env.sessions.calls[0][2:] == ("boss", None)
    events = [e["event_type"] for e in env.audit.entries]
    assert events == ["session.all_search_ai_started", "session.all_search_ai_finished"]
    with pytest.raises(ConflictError, match="no stored posts left"):  # everything was read
        env.service.start_ai("boss", H)


def test_an_ai_failure_keeps_the_reason_and_the_posts_stay_for_a_retry(env):
    env.store.cands = [cand("심의 일정")]
    env.sessions.error = ServiceError("the AI ran out of room")
    env.service.start_ai("boss", H)
    job = env.last()
    assert (job.status, job.error) == ("failed", "the AI ran out of room")
    env.sessions.error = None
    env.service.start_ai("boss", H)  # the failed run's posts are not marked read
    assert env.last().status == "done"


def test_the_ai_step_needs_the_ai_and_can_be_stopped_between_calls():
    off = Env(ai=False)
    off.store.cands = [cand("심의 일정")]
    with pytest.raises(UnavailableError):
        off.service.start_ai("boss", H)
    on = Env()
    on.store.cands = [cand(f"심의 일정 {i}") for i in range(10)]
    on.store.chars = {c.post.version_id: 8000 for c in on.store.cands}

    def stop_after_first(*_):
        on.store.request_stop(on.last().id)
        return {"documents_read": 7, "proposed": 1, "skipped": 0}

    on.sessions.propose_from_posts = lambda *a: (on.sessions.calls.append(a), stop_after_first())[1]
    on.service.start_ai("boss", H)
    job = on.last()
    assert job.status == "stopped" and job.progress["batches_done"] == 1


def test_open_jobs_are_failed_at_a_restart_with_a_reason(env):
    env.store.create_job(H, "boss", "read")
    assert env.service.fail_unfinished() == 1
    assert "restarted" in env.last().error


def test_the_admin_routes_need_an_admin_and_hide_the_server_side_post_ids(env):
    from fastapi.testclient import TestClient

    from irb_watch.api.deps import get_all_search_service, get_user_store, get_verifier
    from irb_watch.main import app
    from tests.test_admin_users_api import FakeVerifier

    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: env.users
    app.dependency_overrides[get_all_search_service] = lambda: env.service
    client = TestClient(app)
    boss, user = {"Authorization": "Bearer tok-boss"}, {"Authorization": "Bearer tok-user"}
    try:
        assert client.post(f"/admin/hospitals/{H}/all-search/read", headers=user).status_code == 403
        env.scans.plan = {1: [batch(3, 0, 3)], 2: [batch(0, 0, 0)]}
        started = client.post(f"/admin/hospitals/{H}/all-search/read", headers=boss)
        assert started.status_code == 202 and started.json()["stage"] == "read"
        env.store.cands = [cand("심의 일정")]
        assert client.get(f"/admin/hospitals/{H}/all-search/preview", headers=boss).json()["posts"] == 1
        ai = client.post(f"/admin/hospitals/{H}/all-search/ai", headers=boss).json()
        assert ai["stage"] == "ai" and "read_versions" not in ai["progress"]
        assert client.get(f"/admin/all-search-jobs/{ai['id']}", headers=boss).json()["status"] == "done"
        assert client.post(f"/admin/all-search-jobs/{ai['id']}/stop", headers=boss).status_code == 409  # not running
        assert [j["stage"] for j in client.get(f"/admin/hospitals/{H}/all-search", headers=boss).json()] == ["ai", "read"]
    finally:
        app.dependency_overrides.clear()
