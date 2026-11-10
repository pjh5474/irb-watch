"""Big attachments are skipped (the post is kept), and an admin can mark file warnings as OK:
the same warning about the same files then stays quiet on later scans; other text or a
replaced file flags the post again."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import get_review_service, get_user_store, get_verifier
from irb_watch.core.detect import Observation, ScanInput, detect_changes
from irb_watch.core.models import AttachmentRef, DownloadedFile
from irb_watch.core.state import CURRENT, NEEDS_REVIEW, FileContent, VersionContent, warning_key
from irb_watch.crawlers.attachments import observe_files
from irb_watch.crawlers.errors import FileTooLargeError
from irb_watch.main import app
from irb_watch.parsers.errors import TOO_LARGE, UNREADABLE
from irb_watch.services.errors import AuditUnavailableError, ConflictError, NotFoundError
from irb_watch.services.reviews import (
    ReviewRow,
    ReviewService,
    is_file_warning,
    settled_values,
    settles,
)
from tests.fakes import FakeAudit, FakeStore
from tests.test_admin_users_api import FakeVerifier

NOW = datetime(2026, 11, 10, tzinfo=UTC)
LIMIT = 20_000_000


# -- big files ---------------------------------------------------------------------------


def downloader(big: set[str]):
    def download(ref: AttachmentRef, max_bytes: int) -> DownloadedFile:
        if ref.filename in big:
            raise FileTooLargeError(f"too large: {ref.filename}")
        return DownloadedFile(ref.filename, b"plain", "s" * 64)

    return download


def test_a_file_over_the_limit_is_listed_not_downloaded_and_the_rest_is_kept():
    refs = [AttachmentRef("big.pdf", "45.2MB", "k1"), AttachmentRef("small.txt", "1KB", "k2")]
    stored = []
    files, warnings = observe_files(refs, downloader({"big.pdf"}), LIMIT, lambda s, n, c: stored.append(n) or s)
    big, small = files
    assert big.hash_only_reason == TOO_LARGE and big.storage_path is None and big.text is None
    assert "larger than 20 MB (the site says 45.2MB)" in big.note
    assert small.filename == "small.txt"
    assert stored == ["small.txt"]  # nothing stored for the big one
    assert warnings == [f"big.pdf: {big.note}"]


def big_record(size, stable_id=None):
    ref = AttachmentRef("big.pdf", size, "k", stable_id=stable_id)
    files, _ = observe_files([ref], downloader({"big.pdf"}), LIMIT, lambda *a: "")
    return files[0]


def test_the_same_big_file_is_unchanged_and_a_new_size_is_a_change():
    assert big_record("45.2MB").sha256 == big_record("45.2MB").sha256
    assert big_record("45.2MB").sha256 != big_record("47.0MB").sha256


def test_a_replaced_big_file_with_no_size_shown_is_still_a_change_on_rbay():
    # R-BAY sometimes shows no size; its download key changes when the file is replaced.
    assert big_record("", "key-old").sha256 == big_record("", "key-old").sha256
    assert big_record("", "key-old").sha256 != big_record("", "key-new").sha256


# -- OK until the warning changes ----------------------------------------------------------

WARN = "big.pdf: not downloaded: larger than 20 MB"


def flagged(sha="a" * 64, name="big.pdf"):
    return FileContent(name, "45MB", sha, None, TOO_LARGE, None, "not downloaded: larger than 20 MB")


def observe(warnings=(), files=()):
    return Observation("1", "http://site/1", None, VersionContent("T", "body", tuple(files)), tuple(warnings))


def scan_once(store, obs, day):
    return detect_changes(store, ScanInput([obs], NOW.replace(day=day), is_first_scan=not store))


def test_a_warning_marked_ok_stays_quiet_until_it_changes():
    store = {}
    first = scan_once(store, observe([WARN], [flagged()]), 1)
    assert store["1"].status == NEEDS_REVIEW and first.needs_review == {"1": WARN}

    store["1"].acknowledged_key = warning_key(WARN, (flagged(),))  # what the admin's OK stores
    again = scan_once(store, observe([WARN], [flagged()]), 2)
    assert store["1"].status == CURRENT and again.needs_review == {}

    other = "b.hwp: the file is damaged"
    changed = scan_once(store, observe([WARN, other], [flagged()]), 3)
    assert store["1"].status == NEEDS_REVIEW and changed.needs_review == {"1": f"{WARN}; {other}"}


def test_a_replaced_file_with_the_same_problem_asks_again():
    store = {}
    scan_once(store, observe([WARN], [flagged("a" * 64)]), 1)
    store["1"].acknowledged_key = warning_key(WARN, (flagged("a" * 64),))
    replaced = scan_once(store, observe([WARN], [flagged("b" * 64)]), 2)  # same text, new file
    assert store["1"].status == NEEDS_REVIEW and replaced.needs_review == {"1": WARN}


# -- which reasons are file warnings ---------------------------------------------------------

BIG = flagged()
BROKEN = FileContent("pack.zip", "1MB", "c" * 64, None, UNREADABLE, None, "the zip file is damaged (bad header)")
FINE = FileContent("a.pdf", "1MB", "d" * 64, "text")


def test_only_the_files_own_warnings_in_their_order_count():
    both = f"big.pdf: {BIG.note}; pack.zip: {BROKEN.note}"
    assert is_file_warning(f"big.pdf: {BIG.note}", (BIG, FINE))
    assert is_file_warning(both, (BIG, BROKEN))
    assert not is_file_warning("detail page 7: article.board-view not found", (BIG,))  # a failed post
    assert not is_file_warning(f"big.pdf: {BIG.note}; something else", (BIG,))
    assert not is_file_warning(f"pack.zip: {BROKEN.note}; big.pdf: {BIG.note}", (BIG, BROKEN))  # wrong order
    assert not is_file_warning(f"big.pdf: {BIG.note}", (BIG, BROKEN))  # one flagged file missing
    assert not is_file_warning(f"big.pdf: {BIG.note}; big.pdf: {BIG.note}", (BIG,))  # repeated
    assert not is_file_warning("big.pdf: ", (BIG,))  # no note
    assert not is_file_warning(None, (BIG,))


def test_a_reworded_note_is_still_a_file_warning():
    # The stored note says 20 MB; a later scan (limit raised) wrote 30 MB without a new version.
    assert is_file_warning("big.pdf: not downloaded: larger than 30 MB", (BIG,))


# -- the review service --------------------------------------------------------------------


def row(reason, files=(BIG, FINE), status="needs_review", document_id=1):
    return ReviewRow(document_id, 4, "이화목동 공지", "제목", status, reason, files)


class Store:
    def __init__(self, rows, accept=True):
        self.rows = {r.document_id: r for r in rows}
        self.accept = accept
        self.acked = []
        self.undone = []

    def needing_review(self, limit):
        return [r for r in self.rows.values() if r.status == "needs_review"][:limit]

    def get(self, document_id):
        return self.rows.get(document_id)

    def acknowledge(self, document_id, reason, key, admin_id, now):
        current = self.rows[document_id]
        if not self.accept or current.status != "needs_review" or current.review_reason != reason:
            return False
        self.acked.append((document_id, reason, key, admin_id))
        self.rows[document_id] = replace(current, status="current", review_reason=None)
        return True

    def undo(self, document_id, reason):
        self.undone.append((document_id, reason))
        self.rows[document_id] = replace(self.rows[document_id], status="needs_review", review_reason=reason)

    def settle_source(self, source_id):
        if getattr(self, "settle_error", None):
            raise self.settle_error
        self.settled = [*getattr(self, "settled", []), source_id]


def test_after_an_ok_the_source_status_is_brought_up_to_date():
    store = Store([row(f"big.pdf: {BIG.note}")])
    ReviewService(store, FakeAudit()).acknowledge("admin-1", 1)
    assert store.settled == [4]


def test_a_failing_source_update_never_undoes_the_ok():
    store = Store([row(f"big.pdf: {BIG.note}")])
    store.settle_error = RuntimeError("database hiccup")
    ReviewService(store, FakeAudit()).acknowledge("admin-1", 1)
    assert store.acked and store.undone == []


def test_which_source_results_an_ok_may_settle():
    per_post = "post 1842962: big.pdf: not downloaded"
    assert settles("needs_review", per_post, None)
    assert not settles("needs_review", "R-BAY session ended (got /login)", None)  # whole scan
    assert not settles("needs_review", "board REGLA: list table not found", None)
    assert not settles("needs_review", per_post, "login rejected")  # paused
    assert not settles("ok", None, None)


def test_the_source_turns_green_when_nothing_is_left_or_lists_what_is():
    assert settled_values([]) == {"last_scan_status": "ok", "last_scan_error": None}
    assert settled_values([("53790", "layout changed")]) == {"last_scan_error": "post 53790: layout changed"}


def test_an_admin_marks_file_warnings_ok_and_it_is_logged():
    reason = f"big.pdf: {BIG.note}"
    store, audit = Store([row(reason)]), FakeAudit()
    service = ReviewService(store, audit, lambda: NOW)
    assert service.list()[0].file_warning
    service.acknowledge("admin-1", 1)
    assert store.acked == [(1, reason, warning_key(reason, (BIG, FINE)), "admin-1")]
    assert audit.entries[-1]["event_type"] == "document.review_acknowledged"
    assert service.list() == []


def test_a_failed_post_cannot_be_marked_ok():
    service = ReviewService(Store([row("detail page 7: layout changed")]), FakeAudit())
    assert not service.list()[0].file_warning
    with pytest.raises(ConflictError, match="only file warnings"):
        service.acknowledge("admin-1", 1)


def test_an_ok_that_cannot_be_logged_is_undone():
    reason = f"big.pdf: {BIG.note}"
    store = Store([row(reason)])
    with pytest.raises(AuditUnavailableError):
        ReviewService(store, FakeAudit(fail_required=True)).acknowledge("admin-1", 1)
    assert store.undone == [(1, reason)] and store.rows[1].status == "needs_review"


def test_no_audit_entry_for_an_ok_refused_because_a_scan_changed_the_warning():
    audit = FakeAudit()
    with pytest.raises(ConflictError, match="changed meanwhile"):
        ReviewService(Store([row(f"big.pdf: {BIG.note}")], accept=False), audit).acknowledge("admin-1", 1)
    assert audit.entries == []  # the append-only log never shows an OK that did not happen


def test_unknown_or_already_clear_documents():
    service = ReviewService(Store([row(None, status="current")]), FakeAudit())
    with pytest.raises(NotFoundError):
        service.acknowledge("admin-1", 99)
    with pytest.raises(ConflictError, match="does not need review"):
        service.acknowledge("admin-1", 1)


# -- the API --------------------------------------------------------------------------------


@pytest.fixture
def api():
    users = FakeStore()
    users.add("admin-1", "boss@example.com", role="admin")
    users.add("user-1", "plain@example.com")
    store = Store([row(f"big.pdf: {BIG.note}"), row("layout changed", document_id=2)])
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: users
    app.dependency_overrides[get_review_service] = lambda: ReviewService(store, FakeAudit())
    yield TestClient(app), store
    app.dependency_overrides.clear()


def test_review_endpoints(api):
    client, store = api
    admin = {"Authorization": "Bearer tok-admin-1"}
    listed = client.get("/admin/documents/reviews", headers=admin).json()
    assert [(r["document_id"], r["file_warning"]) for r in listed] == [(1, True), (2, False)]
    assert client.post("/admin/documents/1/review-ok", headers=admin).status_code == 200
    assert client.post("/admin/documents/2/review-ok", headers=admin).status_code == 409
    user = {"Authorization": "Bearer tok-user-1"}
    assert client.get("/admin/documents/reviews", headers=user).status_code == 403
    assert client.post("/admin/documents/1/review-ok", headers=user).status_code == 403
