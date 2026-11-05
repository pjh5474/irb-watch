from datetime import UTC, datetime, timedelta

import pytest

from irb_watch.crawlers.errors import FetchError, IncompleteScanError, LayoutChangedError
from irb_watch.crawlers.snuh_reader import ScanReading
from irb_watch.services.errors import ConflictError, NotFoundError
from irb_watch.services.scan import ScanService
from tests.fakes import FakeAudit
from tests.fakes_scan import FakeFileStorage, FakeReader, FakeScanRepo, make_obs, make_source

T0 = datetime(2026, 11, 5, tzinfo=UTC)


class Harness:
    def __init__(self):
        self.repo = FakeScanRepo()
        self.repo.add_source(make_source(1))
        self.storage = FakeFileStorage()
        self.audit = FakeAudit()
        self.reader = FakeReader()
        self.day = 0
        self.service = ScanService(
            self.repo,
            self.storage,
            lambda source: self.reader,
            self.audit,
            clock=lambda: T0 + timedelta(days=self.day),
        )

    def scan(self, *observations, failures=None, unreadable=(), day=None, **reader_kw):
        if day is not None:
            self.day = day
        self.reader = FakeReader(
            ScanReading(
                observations=list(observations),
                failures=failures or {},
                unreadable_ids=set(unreadable),
            ),
            **reader_kw,
        )
        return self.service.scan_source(1, "admin-1")

    def events(self, prefix="document."):
        return [e["event_type"] for e in self.audit.entries if e["event_type"].startswith(prefix)]


@pytest.fixture
def h():
    return Harness()


# ---- claiming ------------------------------------------------------------------------


def test_unknown_source(h):
    with pytest.raises(NotFoundError):
        h.service.begin_scan(99)


def test_inactive_source_is_refused(h):
    h.repo.add_source(make_source(2, is_active=False))
    with pytest.raises(ConflictError, match="inactive"):
        h.service.begin_scan(2)


def test_second_scan_while_running_is_refused(h):
    h.service.begin_scan(1)
    with pytest.raises(ConflictError, match="already running"):
        h.service.begin_scan(1)


def test_claim_is_released_after_the_scan(h):
    h.scan(make_obs("1"))
    assert 1 not in h.repo.locked
    h.service.begin_scan(1)  # can be claimed again


# ---- baseline, new, updated, removed --------------------------------------------------


def test_first_scan_is_a_silent_baseline(h):
    summary = h.scan(make_obs("1"), make_obs("2"))
    assert summary.status == "ok" and summary.baseline and summary.new == 0
    assert h.events() == [] and h.events("scan.") == ["scan.finished"]
    assert set(h.repo.history[1]) == {"1", "2"}
    assert h.repo.sources[1].last_scan_at is not None


def test_later_scan_logs_new_updated_and_title_only_priority(h):
    h.scan(make_obs("1", title="Old"), make_obs("2"))
    summary = h.scan(make_obs("1", title="New"), make_obs("2"), make_obs("3"), day=1)
    assert (summary.new, summary.updated) == (1, 1)
    by_type = {e["event_type"]: e for e in h.audit.entries if e["category"] == "document"}
    assert by_type["document.updated"]["priority"] == "low"
    assert by_type["document.updated"]["payload"]["title_changed"] is True
    assert by_type["document.new"]["title"] == "Title"
    assert by_type["document.new"]["document_id"] == h.repo.doc_ids[(1, "3")]
    assert by_type["document.new"]["source_id"] == 1


def test_history_is_kept_and_the_next_scan_works_from_the_latest_version(h):
    h.scan(make_obs("1", body="v1"))
    h.scan(make_obs("1", body="v2"), day=1)
    unchanged = h.scan(make_obs("1", body="v2"), day=2)
    assert unchanged.updated == 0
    assert [v.version_no for v in h.repo.history[1]["1"].versions] == [1, 2]


def test_removal_needs_two_scans(h):
    h.scan(make_obs("1"), make_obs("2"))
    first = h.scan(make_obs("1"), day=1)
    second = h.scan(make_obs("1"), day=2)
    assert first.removed == 0 and second.removed == 1
    assert h.events() == ["document.removed"]


# ---- whole-scan failures --------------------------------------------------------------


@pytest.mark.parametrize(
    "error, status",
    [
        (LayoutChangedError("list markup changed"), "needs_review"),
        (IncompleteScanError("read 3 of 7"), "needs_review"),
        (FetchError("timeout"), "failed"),
        (RuntimeError("boom"), "failed"),
    ],
)
def test_whole_scan_failure_changes_nothing_and_is_recorded(h, error, status):
    h.scan(make_obs("1"))
    summary = h.scan(day=1, error=error)
    assert summary.status == status and summary.error
    assert h.repo.sources[1].last_scan_status == status
    assert set(h.repo.history[1]) == {"1"} and h.repo.history[1]["1"].miss_count == 0
    assert 1 not in h.repo.locked
    assert h.events("scan.")[-1] in ("scan.needs_review", "scan.failed")


def test_failed_first_scan_does_not_use_up_the_baseline(h):
    h.scan(day=0, error=FetchError("network down"))
    assert h.repo.sources[1].last_scan_at is None
    summary = h.scan(make_obs("1"), make_obs("2"), day=1)
    assert summary.baseline and summary.new == 0 and h.events() == []


def test_storage_failure_is_a_failed_scan_and_releases_the_claim(h):
    h.repo.fail_persist = True
    summary = h.scan(make_obs("1"))
    assert summary.status == "failed" and 1 not in h.repo.locked
    assert h.repo.sources[1].last_scan_at is None


def test_reader_is_always_closed(h):
    h.scan(make_obs("1"))
    assert h.reader.closed
    h.scan(day=1, error=FetchError("x"))
    assert h.reader.closed


# ---- per-post failures ------------------------------------------------------------------


def test_failed_post_is_flagged_while_the_rest_is_processed(h):
    h.scan(make_obs("1"), make_obs("2"))
    summary = h.scan(make_obs("1", body="changed"), failures={"2": "download failed"}, day=1)
    assert summary.status == "needs_review" and summary.posts_needing_review == 1
    assert "download failed" in summary.error
    assert summary.updated == 1
    assert h.repo.history[1]["2"].status == "needs_review"
    assert h.repo.history[1]["2"].miss_count == 0
    assert h.repo.sources[1].last_scan_status == "needs_review"


def test_secret_posts_are_not_counted_as_missing(h):
    h.scan(make_obs("1"), make_obs("2"))
    for day in (1, 2, 3):
        h.scan(make_obs("1"), unreadable=["2"], day=day)
    assert h.repo.history[1]["2"].status == "current"


# ---- users can see why something needs review ---------------------------------------------


def with_warning(external_id="1", warning="bad.hwp: HWP is password-protected", **kw):
    base = make_obs(external_id, **kw)
    return type(base)(base.external_id, base.source_url, base.posted_on, base.content, (warning,))


def review_entries(h):
    return [e for e in h.audit.entries if e["event_type"] == "document.needs_review"]


def test_unreadable_attachment_is_announced_once_with_its_reason(h):
    h.scan(make_obs("1", title="Form"))
    first = h.scan(with_warning("1", title="Form"), day=1)
    assert first.status == "needs_review" and first.posts_needing_review == 1

    [entry] = review_entries(h)
    assert entry["category"] == "document"  # visible to subscribers, not admin-only
    assert entry["title"] == "Form" and "password-protected" in entry["summary"]
    assert entry["document_id"] == h.repo.doc_ids[(1, "1")] and entry["source_id"] == 1
    assert h.repo.history[1]["1"].review_reason == "bad.hwp: HWP is password-protected"

    h.scan(with_warning("1", title="Form"), day=2)
    h.scan(with_warning("1", title="Form"), day=3)
    assert len(review_entries(h)) == 1  # not repeated while nothing changes


def test_a_changed_reason_is_announced_again(h):
    h.scan(make_obs("1"))
    h.scan(with_warning("1", warning="a.hwp: HWP is password-protected"), day=1)
    h.scan(with_warning("1", warning="a.hwp: HWP is corrupt"), day=2)
    assert [e["summary"] for e in review_entries(h)] == [
        "a.hwp: HWP is password-protected",
        "a.hwp: HWP is corrupt",
    ]


def test_a_problem_that_returns_after_being_fixed_is_announced_again(h):
    h.scan(make_obs("1"))
    h.scan(with_warning("1"), day=1)
    h.scan(make_obs("1"), day=2)  # fixed
    assert h.repo.history[1]["1"].review_reason is None
    h.scan(with_warning("1"), day=3)
    assert len(review_entries(h)) == 2


def test_post_level_failure_is_announced_with_its_reason(h):
    h.scan(make_obs("1", title="Known post"))
    h.scan(failures={"1": "detail page 1: article.board-view not found"}, day=1)
    [entry] = review_entries(h)
    assert entry["title"] == "Known post" and "article.board-view" in entry["summary"]
    assert h.repo.history[1]["1"].review_reason.startswith("detail page 1")
    h.scan(failures={"1": "detail page 1: article.board-view not found"}, day=2)
    assert len(review_entries(h)) == 1


def test_failure_of_a_post_never_seen_before_still_gets_a_notice(h):
    h.scan(make_obs("1"))
    h.scan(make_obs("1"), failures={"9": "download failed"}, day=1)
    [entry] = review_entries(h)
    assert entry["title"] == "Post 9" and entry["document_id"] is None


# ---- originals are uploaded only when the hash is new ------------------------------------


def test_files_are_uploaded_only_for_new_hashes(h):
    h.repo.stored_hashes.add("known")
    h.scan(
        make_obs("1"),
        sink_calls=[("known", "a.pdf", b"a"), ("fresh", "b.pdf", b"b"), ("fresh", "c.pdf", b"b")],
    )
    assert h.storage.puts == ["fresh"]
    assert h.reader.sink_results == ["known", "fresh", "fresh"]
