import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import get_runner, get_verifier
from irb_watch.crawlers.errors import (
    FetchError,
    LayoutChangedError,
    LoginRejectedError,
    SiteUnavailableError,
    TransientFetchError,
)
from irb_watch.main import app
from irb_watch.scheduler import ScanScheduler, next_daily_run
from irb_watch.services.errors import ConflictError
from irb_watch.services.runner import CATCH_UP, MANUAL, SCHEDULED, ScanRunner, site_of
from irb_watch.services.scan import FAILED, NEEDS_REVIEW, OK, ScanService, ScanSummary
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_scan import FakeFileStorage, FakeReader, FakeScanRepo, make_source
from tests.test_admin_users_api import FakeVerifier

NOW = datetime(2026, 11, 6, 6, tzinfo=UTC)


def src(source_id, host="hrpp.snuh.org", **kw):
    return make_source(
        source_id, url=f"https://{host}/hrpp/bbs/list.do?key={source_id}", name=f"S{source_id}", **kw
    )


class FakeScans:
    """begin_scan/run_scan with scripted outcomes; tracks concurrency per site."""

    def __init__(self, sources, outcomes=None, hold=None):
        self.sources = {s.id: s for s in sources}
        self.outcomes = outcomes or {}
        self.order: list[int] = []
        self.active: dict[str, int] = {}
        self.max_active: dict[str, int] = {}
        self.lock = threading.Lock()
        self.hold = hold  # optional callable(source) run inside run_scan

    def begin_scan(self, source_id):
        outcome = self.outcomes.get(source_id)
        if isinstance(outcome, ConflictError):
            raise outcome
        return self.sources[source_id]

    def run_scan(self, source, actor_id):
        site = site_of(source)
        with self.lock:
            self.order.append(source.id)
            self.active[site] = self.active.get(site, 0) + 1
            self.max_active[site] = max(self.max_active.get(site, 0), self.active[site])
        try:
            if self.hold:
                self.hold(source)
            outcome = self.outcomes.get(source.id, ScanSummary(source.id, OK))
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            with self.lock:
                self.active[site] -= 1


def make_runner(sources, scans=None, clock=lambda: NOW):
    scans = scans or FakeScans(sources)
    audit = FakeAudit()
    retries = []
    runner = ScanRunner(
        lambda: list(sources),
        lambda sid: next((s for s in sources if s.id == sid), None),
        scans,
        audit,
        lambda sid, due: retries.append((sid, due)),
        clock,
    )
    return runner, scans, audit, retries


def kinds(audit):
    return [e["event_type"] for e in audit.entries]


# ---- who runs, in what shape --------------------------------------------------------------


def test_sites_run_in_parallel_but_boards_of_one_site_run_one_by_one():
    sources = [src(1), src(2), src(3), src(4, host="other.example.org")]
    barrier = threading.Barrier(2, timeout=5)

    def hold(source):
        if source.id in (1, 4):  # first board of each site must be running at the same time
            barrier.wait()

    scans = FakeScans(sources, hold=hold)
    runner, _, _, _ = make_runner(sources, scans)
    report = runner.run_all()
    assert set(report.scanned) == {1, 2, 3, 4}
    assert scans.max_active["hrpp.snuh.org"] == 1  # never two boards of one site together
    assert scans.order.index(1) < scans.order.index(2) < scans.order.index(3)


def test_inactive_sources_are_ignored():
    sources = [src(1), src(2, is_active=False)]
    runner, scans, _, _ = make_runner(sources)
    assert set(runner.run_all().scanned) == {1}


def test_one_crashing_source_does_not_stop_the_rest_of_its_site():
    sources = [src(1), src(2)]
    scans = FakeScans(sources, outcomes={1: RuntimeError("boom")})
    runner, _, audit, _ = make_runner(sources, scans)
    report = runner.run_all()
    assert report.scanned == {1: FAILED, 2: OK}
    assert "scan.failed" in kinds(audit)


def test_running_scan_is_skipped_with_a_recorded_reason():
    sources = [src(1), src(2)]
    scans = FakeScans(sources, outcomes={1: ConflictError("a scan of this source is already running")})
    runner, _, audit, _ = make_runner(sources, scans)
    report = runner.run_all()
    assert report.skipped == {1: "a scan of this source is already running"}
    assert report.scanned == {2: OK}
    skip = next(e for e in audit.entries if e["event_type"] == "scan.skipped")
    assert skip["source_id"] == 1 and "already running" in skip["payload"]["reason"]


@pytest.mark.parametrize("trigger", [SCHEDULED, CATCH_UP, MANUAL])
def test_source_paused_after_rejected_login_is_skipped_by_every_full_run(trigger):
    sources = [src(1, scans_paused_reason="login rejected: still on the login page"), src(2)]
    runner, scans, audit, _ = make_runner(sources)
    report = runner.run_all(trigger)
    assert 1 not in report.scanned and "paused after a rejected login" in report.skipped[1]
    assert scans.order == [2]
    assert "scan.skipped" in kinds(audit)


def test_only_one_full_run_at_a_time_and_the_second_is_recorded():
    sources = [src(1)]
    inside, release = threading.Event(), threading.Event()

    def hold(source):
        inside.set()
        assert release.wait(5)

    runner, _, audit, _ = make_runner(sources, FakeScans(sources, hold=hold))
    first = threading.Thread(target=runner.run_all)
    first.start()
    assert inside.wait(5) and runner.is_running()
    second = runner.run_all(MANUAL)
    assert second.skipped == {0: "another full run is still in progress"}
    release.set()
    first.join(5)
    assert not runner.is_running()
    assert "scan.run_skipped" in kinds(audit)


def test_each_full_run_leaves_a_summary_entry():
    sources = [src(1), src(2)]
    scans = FakeScans(sources, outcomes={2: ScanSummary(2, FAILED, error="x")})
    runner, _, audit, _ = make_runner(sources, scans)
    runner.run_all()
    done = next(e for e in audit.entries if e["event_type"] == "scan.run_finished")
    assert done["payload"]["scanned"] == 2 and done["payload"]["failed"] == 1


# ---- catch-up after downtime ------------------------------------------------------------------


def test_catch_up_scans_only_sources_not_saved_for_over_24_hours():
    fresh = (NOW - timedelta(hours=2)).isoformat()
    stale = (NOW - timedelta(hours=30)).isoformat()
    sources = [src(1, last_scan_at=fresh), src(2, last_scan_at=stale), src(3, last_scan_at=None)]
    runner, scans, audit, _ = make_runner(sources)
    report = runner.run_all(CATCH_UP)
    assert sorted(report.scanned) == [2, 3]
    assert "scan.skipped" not in kinds(audit)  # a source that is not overdue is not news


def test_scheduled_run_scans_everything_regardless_of_age():
    sources = [src(1, last_scan_at=(NOW - timedelta(hours=1)).isoformat()), src(2)]
    runner, _, _, _ = make_runner(sources)
    assert sorted(runner.run_all(SCHEDULED).scanned) == [1, 2]


# ---- retry once, only for a site that was down --------------------------------------------------


def test_site_unavailable_failure_plans_one_retry_in_an_hour():
    sources = [src(1), src(2), src(3)]
    scans = FakeScans(
        sources,
        outcomes={
            1: ScanSummary(1, FAILED, error="site unavailable", retryable=True),
            2: ScanSummary(2, FAILED, error="HTTP 404"),  # a real fault: no retry
            3: ScanSummary(3, NEEDS_REVIEW, error="layout changed"),
        },
    )
    runner, _, audit, retries = make_runner(sources, scans)
    report = runner.run_all()
    assert retries == [(1, NOW + timedelta(hours=1))]
    assert report.retries_planned == [1]
    assert "scan.retry_planned" in kinds(audit)


def test_a_retry_never_plans_another_retry():
    sources = [src(1)]
    scans = FakeScans(sources, outcomes={1: ScanSummary(1, FAILED, error="down", retryable=True)})
    runner, _, _, retries = make_runner(sources, scans)
    runner.run_retry(1)
    assert retries == []
    assert scans.order == [1]


def test_retry_skips_a_source_that_was_paused_or_removed_meanwhile():
    paused = src(1, scans_paused_reason="login rejected")
    inactive = src(2, is_active=False)
    runner, scans, audit, _ = make_runner([paused, inactive])
    assert 1 in runner.run_retry(1).skipped
    assert runner.run_retry(2).scanned == {} and runner.run_retry(99).scanned == {}
    assert scans.order == []


# ---- how failures are classified (real ScanService, fake reader) ------------------------------


def classify(error):
    repo = FakeScanRepo()
    repo.add_source(make_source(1))
    reader = FakeReader(error=error)
    service = ScanService(
        repo, FakeFileStorage(), lambda s: reader, FakeAudit(), clock=lambda: NOW
    )
    return service.scan_source(1, None), repo


@pytest.mark.parametrize(
    "error", [SiteUnavailableError("site unavailable"), TransientFetchError("HTTP 502 (gave up)")]
)
def test_temporary_site_trouble_is_retryable(error):
    summary, repo = classify(error)
    assert summary.status == FAILED and summary.retryable and summary.paused_reason is None
    assert repo.get_source(1).scans_paused_reason is None


@pytest.mark.parametrize("error", [FetchError("HTTP 404 for x"), FetchError("blocked URL")])
def test_other_fetch_errors_are_not_retryable(error):
    summary, _ = classify(error)
    assert summary.status == FAILED and not summary.retryable


def test_rejected_login_pauses_the_source_and_is_not_retryable():
    summary, repo = classify(LoginRejectedError("login rejected: still on the login page"))
    assert summary.status == NEEDS_REVIEW and not summary.retryable
    assert "login rejected" in repo.get_source(1).scans_paused_reason


def test_layout_change_needs_review_but_does_not_pause_daily_scans():
    summary, repo = classify(LayoutChangedError("markup changed"))
    assert summary.status == NEEDS_REVIEW and repo.get_source(1).scans_paused_reason is None


def test_a_later_successful_scan_clears_the_pause():
    repo = FakeScanRepo()
    repo.add_source(make_source(1))
    readers = [FakeReader(error=LoginRejectedError("login rejected: x")), FakeReader()]
    service = ScanService(
        repo, FakeFileStorage(), lambda s: readers.pop(0), FakeAudit(), clock=lambda: NOW
    )
    service.scan_source(1, None)
    assert repo.get_source(1).scans_paused_reason
    service.scan_source(1, "admin-1")  # a person starts a scan by hand
    assert repo.get_source(1).scans_paused_reason is None


def test_paused_source_is_skipped_by_the_runner_using_real_state():
    repo = FakeScanRepo()
    repo.add_source(make_source(1))
    service = ScanService(
        repo, FakeFileStorage(), lambda s: FakeReader(error=LoginRejectedError("login rejected: x")),
        FakeAudit(), clock=lambda: NOW,
    )
    runner = ScanRunner(
        lambda: list(repo.sources.values()), repo.get_source, service, FakeAudit(), lambda *a: None,
        lambda: NOW,
    )
    assert runner.run_all().scanned == {1: NEEDS_REVIEW}
    second = runner.run_all()  # next day: must not try the login again
    assert second.scanned == {} and 1 in second.skipped


# ---- scheduler ------------------------------------------------------------------------------------


def test_daily_run_is_06_00_korea_time():
    after = datetime(2026, 11, 6, 12, tzinfo=UTC)  # 21:00 KST
    nxt = next_daily_run("0 6 * * *", "Asia/Seoul", after)
    assert nxt.astimezone(UTC) == datetime(2026, 11, 6, 21, tzinfo=UTC)  # 06:00 KST next day
    assert (nxt.hour, nxt.minute) == (6, 0)


def test_scheduler_registers_daily_job_catch_up_and_replaceable_retries(monkeypatch):
    monkeypatch.setattr("irb_watch.scheduler.CATCH_UP_DELAY", timedelta(hours=5))
    runner, _, _, _ = make_runner([src(1)])
    scheduler = ScanScheduler(runner, "0 6 * * *", "Asia/Seoul")
    scheduler.start()
    try:
        assert scheduler.job_ids() == ["daily-scan", "startup-catch-up"]
        scheduler.schedule_retry(7, datetime.now(UTC) + timedelta(hours=1))
        scheduler.schedule_retry(7, datetime.now(UTC) + timedelta(hours=2))  # replaces, not adds
        assert scheduler.job_ids() == ["daily-scan", "retry-7", "startup-catch-up"]
    finally:
        scheduler.stop()


# ---- endpoint -----------------------------------------------------------------------------------


class StubRunner:
    def __init__(self, running=False):
        self.running, self.calls = running, []

    def is_running(self):
        return self.running

    def run_all(self, trigger, actor_id=None):
        self.calls.append((trigger, actor_id))


@pytest.fixture
def api():
    users = FakeStore()
    users.add("admin-1", "boss@example.com", role="admin")
    users.add("user-1", "plain@example.com")
    from irb_watch.api.deps import get_user_store

    runner = StubRunner()
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: users
    app.dependency_overrides[get_runner] = lambda: runner
    yield TestClient(app), runner
    app.dependency_overrides.clear()


def test_run_all_endpoint_is_admin_only(api):
    client, runner = api
    assert client.post("/admin/scans/run-all").status_code == 401
    r = client.post("/admin/scans/run-all", headers={"Authorization": "Bearer tok-user-1"})
    assert r.status_code == 403 and runner.calls == []


def test_run_all_endpoint_starts_a_manual_run_as_the_admin(api):
    client, runner = api
    r = client.post("/admin/scans/run-all", headers={"Authorization": "Bearer tok-admin-1"})
    assert r.status_code == 202 and r.json() == {"status": "started"}
    assert runner.calls == [(MANUAL, "admin-1")]


def test_run_all_endpoint_refuses_while_a_run_is_in_progress(api):
    client, runner = api
    runner.running = True
    r = client.post("/admin/scans/run-all", headers={"Authorization": "Bearer tok-admin-1"})
    assert r.status_code == 409 and runner.calls == []
