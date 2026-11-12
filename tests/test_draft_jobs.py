"""Draft jobs (owner, 2026-11-12; docs/decisions.md "Draft jobs")."""

from datetime import UTC, datetime, timedelta

import pytest

from irb_watch.services.draft_jobs import RESTARTED, DraftJobService
from irb_watch.services.errors import (
    ConflictError,
    NotFoundError,
    TooManyRequestsError,
    UpstreamError,
    ValidationError,
)
from tests.fakes_jobs import FakeJobStore, LaterRunner

NOW = datetime(2026, 11, 12, 1, tzinfo=UTC)


class Result:
    """Stands in for DraftDetail: the new draft's id is read from .draft.id."""

    def __init__(self, draft_id):
        self.draft = type("D", (), {"id": draft_id})()


def make():
    store, runner = FakeJobStore(), LaterRunner()
    return DraftJobService(store, runner, clock=lambda: NOW), store, runner


def ok(result=None):
    return lambda: (lambda: result)


def test_a_job_is_queued_then_done_with_the_new_draft():
    jobs, store, runner = make()
    job = jobs.start("u1", "draft", ok(Result("d-1")))
    assert job.status == "queued" and jobs.list("u1") == [job]
    runner.run()
    done = jobs.get("u1", job.id)
    assert (done.status, done.draft_id, done.error) == ("done", "d-1", None)
    assert done.started_at and done.finished_at
    assert jobs.list("u1") == []  # finished jobs leave the open list
    with pytest.raises(NotFoundError):
        jobs.get("u2", job.id)


def test_a_refusal_in_the_checks_is_answered_at_once_and_leaves_no_job():
    jobs, store, runner = make()

    def refuse():
        raise ValidationError("choose at least one document")

    with pytest.raises(ValidationError):
        jobs.start("u1", "draft", refuse)
    assert store.jobs == {} and runner.waiting == []


def test_a_failure_always_has_a_reason():
    jobs, store, runner = make()

    def upstream():
        raise UpstreamError("the AI could not write the draft (TimeoutError); please try again")

    def crash():
        raise KeyError("secret detail")

    a = jobs.start("u1", "draft", lambda: upstream)
    b = jobs.start("u1", "draft", lambda: crash)
    runner.run()
    assert jobs.get("u1", a.id).error == "the AI could not write the draft (TimeoutError); please try again"
    failed = jobs.get("u1", b.id)
    assert failed.status == "failed" and failed.error == "unexpected error (KeyError); please try again"
    assert "secret" not in failed.error
    assert {j.id for j in jobs.list("u1")} == {a.id, b.id}  # shown for 24 hours


def test_limits_two_per_user_one_per_step_one_revision_per_draft():
    jobs, store, runner = make()
    jobs.start("u1", "draft", ok(), step_id=5)
    with pytest.raises(ConflictError, match="this step"):
        jobs.start("u1", "draft", ok(), step_id=5)
    jobs.start("u1", "revision", ok(), draft_id="d-1")
    with pytest.raises(TooManyRequestsError, match="2 of your drafts"):
        jobs.start("u1", "draft", ok())
    jobs.start("u2", "draft", ok())  # another user is not affected
    runner.run()
    jobs.start("u1", "revision", ok(), draft_id="d-1")
    with pytest.raises(ConflictError, match="revised already"):
        jobs.start("u1", "revision", ok(), draft_id="d-1")
    jobs.start("u1", "revision", ok(), draft_id="d-2")


def test_the_unique_index_catches_a_race_the_check_missed():
    jobs, store, runner = make()
    store.create("u9", "draft", None, 5)  # another request's job on the same step, just made
    with pytest.raises(ConflictError, match="written already"):
        jobs.start("u1", "draft", ok(), step_id=5)


def test_jobs_open_at_server_start_are_failed_and_never_run_again():
    jobs, store, runner = make()
    ran = []
    job = jobs.start("u1", "draft", lambda: (lambda: ran.append(1)))
    done = jobs.start("u2", "draft", ok(Result("d-2")))
    runner.waiting.pop()  # u2's job
    store.update(done.id, {"status": "done"})
    restarted = DraftJobService(store, LaterRunner(), clock=lambda: NOW)
    assert restarted.fail_unfinished() == 1
    failed = restarted.get("u1", job.id)
    assert failed.status == "failed" and failed.error == RESTARTED
    assert restarted.get("u2", done.id).status == "done"
    assert ran == []
    assert restarted.fail_unfinished() == 0


def test_a_job_that_cannot_be_saved_as_finished_does_not_crash_the_worker():
    jobs, store, runner = make()
    job = jobs.start("u1", "draft", ok(Result("d-1")))
    store.fail_update = True
    runner.run()  # must not raise
    store.fail_update = False
    assert jobs.get("u1", job.id).status == "queued"  # failed by the next start, with a reason
    assert jobs.fail_unfinished() == 1


# -- fixes after the code review (2026-11-12) --------------------------------------------------


def test_the_ownership_check_runs_before_any_job_row_exists():
    jobs, store, runner = make()
    prepared = []

    def not_mine():
        raise NotFoundError("step not found")

    with pytest.raises(NotFoundError):
        jobs.start("u1", "draft", lambda: prepared.append(1), step_id=5, owns=not_mine)
    assert store.jobs == {} and prepared == []
    jobs.start("u2", "draft", ok(), step_id=5)  # the owner is not blocked by the stranger


def test_a_job_failed_meanwhile_is_not_turned_into_done_by_a_late_worker():
    jobs, store, runner = make()
    job = jobs.start("u1", "draft", ok(Result("d-1")))
    assert jobs.fail_unfinished() == 1  # for example a restart of this server
    runner.run()  # the old worker finishes late
    late = jobs.get("u1", job.id)
    assert (late.status, late.error, late.draft_id) == ("failed", RESTARTED, None)


def test_requests_at_the_same_moment_cannot_pass_the_two_per_user_limit():
    jobs, store, runner = make()
    real = store.for_user
    calls = []

    def for_user(user_id, since):
        calls.append(1)
        if len(calls) == 1:  # this request's first look: two others are inserted right after it
            found = real(user_id, since)
            store.create("u1", "draft", None, None)
            store.create("u1", "draft", None, None)
            return found
        return real(user_id, since)

    store.for_user = for_user
    prepared = []
    with pytest.raises(TooManyRequestsError):
        jobs.start("u1", "draft", lambda: prepared.append(1))
    assert prepared == [] and len(store.jobs) == 2  # the third gave way, uncounted


def test_old_finished_jobs_are_purged_and_open_ones_kept():
    store, runner = FakeJobStore(), LaterRunner()
    later = NOW + timedelta(days=400)
    jobs = DraftJobService(store, runner, clock=lambda: later)
    old = jobs.start("u1", "draft", ok())
    runner.run()
    still_open = jobs.start("u1", "draft", ok())
    assert jobs.purge_old() == 1
    assert store.get(old.id) is None and store.get(still_open.id) is not None
