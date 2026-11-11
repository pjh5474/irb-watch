"""Manual sources: documents uploaded by hand (owner decisions 2026-11-11)."""

import base64
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import (
    get_alert_service,
    get_manual_service,
    get_source_service,
    get_user_store,
    get_verifier,
)
from irb_watch.core import health as h
from irb_watch.main import app
from irb_watch.services.errors import ConflictError, NotFoundError, ValidationError
from irb_watch.services.manual import MAX_FILES, ManualFile, ManualService
from irb_watch.services.runner import ScanRunner
from irb_watch.services.scan import ScanService
from irb_watch.services.sources import MANUAL_ADAPTER, SourceService
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_scan import FakeFileStorage, FakeScanRepo, FakeSourceStore, make_source
from tests.test_admin_users_api import FakeVerifier

NOW = datetime(2026, 11, 11, 3, 0, tzinfo=UTC)


def f(name: str, text: str) -> ManualFile:
    return ManualFile(name, base64.b64encode(text.encode()).decode())


def manual_source(source_id=7, **kw):
    return make_source(
        source_id, name="Hanyang IRB", adapter=MANUAL_ADAPTER, url="manual:abc", config={}, **kw
    )


class FakeEnricher:
    def __init__(self):
        self.calls = 0

    def enrich(self, events, store):
        self.calls += 1
        return {}


@pytest.fixture
def env():
    repo = FakeScanRepo(FakeAudit())
    repo.add_source(manual_source())
    repo.add_source(make_source(1))  # a scanned SNUH source
    storage = FakeFileStorage()
    enricher = FakeEnricher()
    service = ManualService(
        repo, storage, repo.audit, repo.get_source, enricher=enricher, max_file_bytes=1000, clock=lambda: NOW
    )
    return service, repo, storage, enricher


def events(repo):
    return [c.event_type for c in repo.cards]


# -- creating the source --------------------------------------------------------------------


def test_create_a_manual_source():
    audit = FakeAudit()
    service = SourceService(FakeSourceStore(), audit)
    s = service.create_manual("admin-1", " Hanyang IRB ", members_only=True)
    assert s.adapter == MANUAL_ADAPTER and s.url.startswith("manual:") and s.name == "Hanyang IRB"
    assert s.requires_login is True and s.ai_enabled is False  # like a study
    public = service.create_manual("admin-1", "Other", members_only=False)
    assert public.ai_enabled is True and public.url != s.url
    assert audit.entries[-1]["payload"]["adapter"] == MANUAL_ADAPTER
    with pytest.raises(ValidationError, match="no site to log in"):
        service.update("admin-1", s.id, login_user="someone")


def test_a_manual_source_is_never_scanned():
    repo = FakeScanRepo()
    repo.add_source(manual_source())
    scans = ScanService(repo, FakeFileStorage(), lambda s: None, FakeAudit())
    with pytest.raises(ValidationError, match="not scanned"):
        scans.begin_scan(7)
    runner = ScanRunner(
        lambda: [manual_source()], repo.get_source, scans, FakeAudit(), schedule_retry=lambda *_: None
    )
    report = runner.run_all()
    assert report.scanned == {} and report.skipped == {}


def test_a_manual_source_is_never_stale_but_a_flagged_document_still_counts():
    old = "2026-01-01T00:00:00+00:00"
    assert h.source_health(is_active=True, last_scan_at=old, last_scan_status="ok", manual=True).level == h.OK
    assert h.source_health(is_active=True, last_scan_at=old, last_scan_status="ok").level == h.ATTENTION
    flagged = h.source_health(is_active=True, last_scan_at=old, last_scan_status="needs_review", manual=True)
    assert flagged.level == h.PROBLEM


# -- documents and versions -----------------------------------------------------------------


def test_the_first_upload_is_a_new_document_with_a_card_not_a_silent_baseline(env):
    service, repo, storage, enricher = env
    r = service.create_document("admin-1", 7, "Consent form", "v1 notes", "2026-11-01", [f("a.txt", "one")])
    assert (r.change, r.version_no) == ("new", 1) and r.document_id > 0
    assert events(repo) == ["document.new", "manual.saved"]
    doc = repo.history[7][next(iter(repo.history[7]))]
    assert doc.source_url == "" and doc.posted_on == "2026-11-01"
    assert [x.filename for x in doc.latest.content.files] == ["a.txt"]
    assert storage.puts and enricher.calls == 1
    assert repo.sources[7].last_scan_at == NOW.isoformat() and repo.sources[7].last_scan_status == "ok"
    assert 7 not in repo.locked


def test_a_new_version_keeps_replaces_and_adds_files(env):
    service, repo, _, _ = env
    first = service.create_document("admin-1", 7, "Protocol", "", None, [f("a.txt", "A"), f("b.txt", "B")])
    r = service.add_version(
        "admin-1", 7, first.document_id, None, None, None, keep_files=["a.txt", "b.txt"],
        files=[f("b.txt", "B2"), f("c.txt", "C")],
    )
    assert (r.change, r.version_no) == ("updated", 2)
    doc = repo.history[7][next(iter(repo.history[7]))]
    files = {x.filename: x for x in doc.latest.content.files}
    assert sorted(files) == ["a.txt", "b.txt", "c.txt"]
    assert files["a.txt"].sha256 == doc.versions[0].content.files[0].sha256  # kept as it was
    assert doc.latest.content.title == "Protocol"
    card = next(c for c in repo.cards if c.event_type == "document.updated")
    assert card.payload["files"] == {"b.txt": "replaced", "c.txt": "added"}


def test_files_not_kept_are_removed_and_the_title_can_change(env):
    service, repo, _, _ = env
    first = service.create_document("admin-1", 7, "Protocol", "", None, [f("a.txt", "A"), f("b.txt", "B")])
    service.add_version("admin-1", 7, first.document_id, "Protocol v2", None, None, ["a.txt"], [])
    card = next(c for c in repo.cards if c.event_type == "document.updated")
    assert card.payload["files"] == {"b.txt": "removed"} and card.payload["title_changed"] is True


def test_nothing_changed_is_refused_and_frees_the_lock(env):
    service, repo, _, _ = env
    first = service.create_document("admin-1", 7, "Protocol", "", "2026-11-01", [f("a.txt", "A")])
    count = len(repo.cards)
    with pytest.raises(ConflictError, match="nothing changed"):
        service.add_version("admin-1", 7, first.document_id, None, None, None, ["a.txt"], [])
    with pytest.raises(ConflictError, match="nothing changed"):  # the same file again
        service.add_version("admin-1", 7, first.document_id, None, None, None, [], [f("a.txt", "A")])
    assert len(repo.cards) == count and 7 not in repo.locked


def test_only_the_date_changed_is_saved_without_a_card(env):
    service, repo, _, _ = env
    first = service.create_document("admin-1", 7, "Protocol", "", "2026-11-01", [f("a.txt", "A")])
    r = service.add_version("admin-1", 7, first.document_id, None, None, "2026-11-05", ["a.txt"], [])
    assert r.change == "details" and r.version_no == 1
    assert events(repo)[-1] == "manual.saved" and "document.updated" not in events(repo)


def test_bad_input_is_refused_with_a_reason_and_saves_nothing(env):
    service, repo, storage, _ = env
    cases = [
        (lambda: service.create_document("admin-1", 7, "  ", "", None, []), "title"),
        (lambda: service.create_document("admin-1", 7, "T", "", "11/01/2026", []), "posted date"),
        (lambda: service.create_document("admin-1", 7, "T", "", None, [f("a.txt", "x" * 1001)]), "larger than"),
        (lambda: service.create_document("admin-1", 7, "T", "", None, [f("a.txt", "1"), f("a.txt", "2")]), "two files"),
        (lambda: service.create_document("admin-1", 7, "T", "", None, [f("a.txt", "")]), "empty"),
        (lambda: service.create_document("admin-1", 7, "T", "", None, [f(f"{n}.txt", "x") for n in range(MAX_FILES + 1)]), "at most"),
        (lambda: service.create_document("admin-1", 7, "T", "", None, [f("fake.pdf", "<html>not a pdf</html>")]), "fake.pdf"),
        (lambda: service.create_document("admin-1", 1, "T", "", None, []), "only to a manual source"),
    ]
    for call, reason in cases:
        with pytest.raises(ValidationError, match=reason):
            call()
    assert repo.cards == [] and 7 not in repo.locked


def test_unknown_document_or_kept_file(env):
    service, _, _, _ = env
    first = service.create_document("admin-1", 7, "Protocol", "", None, [f("a.txt", "A")])
    with pytest.raises(NotFoundError):
        service.add_version("admin-1", 7, 999, None, None, None, [], [f("b.txt", "B")])
    with pytest.raises(ValidationError, match="no file named"):
        service.add_version("admin-1", 7, first.document_id, None, None, None, ["zzz.txt"], [])


def test_a_busy_source_answers_conflict(env):
    service, repo, _, _ = env
    repo.locked.add(7)
    with pytest.raises(ConflictError, match="another upload"):
        service.create_document("admin-1", 7, "T", "", None, [])


def test_a_failed_save_leaves_the_status_and_frees_the_lock(env):
    service, repo, _, _ = env
    repo.sources[7] = replace(repo.sources[7], last_scan_status="ok")
    repo.fail_persist = True
    with pytest.raises(RuntimeError):
        service.create_document("admin-1", 7, "T", "", None, [f("a.txt", "A")])
    assert 7 not in repo.locked and repo.sources[7].last_scan_status == "ok" and repo.cards == []


def test_ai_off_sends_nothing_to_the_ai_and_writes_a_template_line(env):
    service, repo, _, enricher = env
    repo.sources[7] = replace(repo.sources[7], ai_enabled=False)
    first = service.create_document("admin-1", 7, "Protocol", "", None, [f("a.txt", "A")])
    service.add_version("admin-1", 7, first.document_id, None, None, None, [], [f("a.txt", "A2")])
    assert enricher.calls == 0
    card = next(c for c in repo.cards if c.event_type == "document.updated")
    assert card.payload["summary_source"] == "template" and card.summary


def test_remove_and_restore(env):
    service, repo, _, _ = env
    first = service.create_document("admin-1", 7, "Protocol", "", None, [f("a.txt", "A")])
    r = service.set_removed("admin-1", 7, first.document_id, True)
    assert r.change == "removed"
    doc = repo.history[7][next(iter(repo.history[7]))]
    assert doc.status == "removed" and doc.removed_at == NOW
    with pytest.raises(ConflictError, match="restore it first"):
        service.add_version("admin-1", 7, first.document_id, "New", None, None, ["a.txt"], [])
    with pytest.raises(ConflictError, match="already marked removed"):
        service.set_removed("admin-1", 7, first.document_id, True)
    assert service.set_removed("admin-1", 7, first.document_id, False).change == "reappeared"
    assert doc.status == "current" and doc.removed_at is None
    assert events(repo).count("document.removed") == 1 and events(repo).count("document.reappeared") == 1


def test_an_unreadable_file_is_kept_and_flags_the_document_and_the_source(env, monkeypatch):
    from irb_watch.parsers import formats

    service, repo, _, _ = env
    real = formats.extract_text

    def locked(name, content):
        if name == "locked.hwp":
            return formats.ExtractResult(None, "unreadable", note="password-protected")
        return real(name, content)

    monkeypatch.setattr("irb_watch.crawlers.attachments.extract_text", locked)
    r = service.create_document("admin-1", 7, "Protocol", "", None, [f("locked.hwp", "x")])
    assert r.warnings == ("locked.hwp: password-protected",)
    assert "document.needs_review" in events(repo)
    assert repo.sources[7].last_scan_status == "needs_review"


# -- the API ---------------------------------------------------------------------------------


def test_the_api(env):
    service, repo, _, _ = env
    users = FakeStore()
    users.add("admin-1", "a@example.com", role="admin")
    users.add("user-1", "u@example.com")
    sources = SourceService(FakeSourceStore(), FakeAudit())
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: users
    app.dependency_overrides[get_manual_service] = lambda: service
    app.dependency_overrides[get_source_service] = lambda: sources
    alerts = FakeAlerts()
    app.dependency_overrides[get_alert_service] = lambda: alerts
    try:
        client = TestClient(app)
        h_admin = {"Authorization": "Bearer tok-admin-1"}
        res = client.post("/admin/sources/manual", headers=h_admin, json={"name": "Hanyang", "members_only": True})
        assert res.status_code == 201 and res.json()["adapter"] == "manual" and res.json()["requires_login"] is True
        body = {"title": "Consent", "files": [{"filename": "a.txt", "content_base64": base64.b64encode(b"A").decode()}]}
        res = client.post("/admin/sources/7/manual-documents", headers=h_admin, json=body)
        assert res.status_code == 201 and res.json()["change"] == "new"
        doc_id = res.json()["document_id"]
        res = client.post(
            f"/admin/sources/7/manual-documents/{doc_id}/versions",
            headers=h_admin,
            json={"keep_files": ["a.txt"]},
        )
        assert res.status_code == 409 and "nothing changed" in res.json()["detail"]
        assert client.post(f"/admin/sources/7/manual-documents/{doc_id}/remove", headers=h_admin).json()["change"] == "removed"
        # every save is followed by the alert check (a refused one is not)
        assert alerts.evaluated == [7, 7]
        h_user = {"Authorization": "Bearer tok-user-1"}
        assert client.post("/admin/sources/manual", headers=h_user, json={"name": "x"}).status_code == 403
        assert client.post("/admin/sources/7/manual-documents", headers=h_user, json=body).status_code == 403
    finally:
        app.dependency_overrides.clear()


class FakeAlerts:
    def __init__(self, fail=False):
        self.evaluated: list[int] = []
        self.fail = fail

    def evaluate_source(self, source_id):
        self.evaluated.append(source_id)
        if self.fail:
            raise RuntimeError("mail down")


def test_oversized_or_undeclared_uploads_are_refused_before_reading():
    from irb_watch.api.admin_manual import MAX_UPLOAD_REQUEST_BYTES

    client = TestClient(app)
    path = "/admin/sources/7/manual-documents"
    res = client.post(path, content=b"{}", headers={"Content-Length": str(MAX_UPLOAD_REQUEST_BYTES + 1)})
    assert res.status_code == 413 and "too large" in res.json()["detail"]
    res = client.post(f"{path}/5/versions", content=b"{}", headers={"Content-Length": str(MAX_UPLOAD_REQUEST_BYTES + 1)})
    assert res.status_code == 413
    # other routes are not affected (this one then fails on the missing login, as usual)
    res = client.post("/admin/sources/manual", content=b"{}", headers={"Content-Length": str(MAX_UPLOAD_REQUEST_BYTES + 1)})
    assert res.status_code != 413


def test_a_mail_problem_never_fails_an_upload(env):
    service, _, _, _ = env
    users = FakeStore()
    users.add("admin-1", "a@example.com", role="admin")
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: users
    app.dependency_overrides[get_manual_service] = lambda: service
    app.dependency_overrides[get_alert_service] = lambda: FakeAlerts(fail=True)
    try:
        body = {"title": "Consent", "files": [{"filename": "a.txt", "content_base64": base64.b64encode(b"A").decode()}]}
        res = TestClient(app).post(
            "/admin/sources/7/manual-documents", headers={"Authorization": "Bearer tok-admin-1"}, json=body
        )
        assert res.status_code == 201
    finally:
        app.dependency_overrides.clear()


# -- fixes after the code review (2026-11-11) -------------------------------------------------


def test_the_same_new_document_twice_is_refused(env):
    service, repo, _, _ = env
    service.create_document("admin-1", 7, "Consent", "", None, [f("a.txt", "A")])
    count = len(repo.cards)
    with pytest.raises(ConflictError, match="already exists"):
        service.create_document("admin-1", 7, "Consent", "", "2026-11-02", [f("a.txt", "A")])
    assert len(repo.cards) == count and 7 not in repo.locked
    # a different file, or a removed twin, is fine
    service.create_document("admin-1", 7, "Consent", "", None, [f("a.txt", "B")])


def test_a_failed_upload_keeps_the_newest_status_not_the_one_read_first(env):
    """This request read the source while it was fine; another upload flagged it meanwhile.
    The failed request must not put the old "ok" back."""
    service, repo, _, _ = env
    stale = repo.sources[7]
    repo.sources[7] = replace(stale, last_scan_status="needs_review", last_scan_error="post m-1: locked")
    service._source = lambda sid: stale  # the read at the start of the request
    repo.fail_persist = True
    with pytest.raises(RuntimeError):
        service.create_document("admin-1", 7, "T", "", None, [f("a.txt", "A")])
    assert repo.sources[7].last_scan_status == "needs_review"
    assert repo.sources[7].last_scan_error == "post m-1: locked"


def test_a_too_large_save_has_a_reason(env):
    from irb_watch.crawlers.errors import ScanTooLargeError

    service, repo, _, _ = env

    def too_large(*_a, **_k):
        raise ScanTooLargeError("scan too large to save safely in one step (25.0 MB, limit 20.0 MB); nothing was saved")

    repo.commit_scan = too_large
    with pytest.raises(ValidationError, match="upload fewer or smaller files"):
        service.create_document("admin-1", 7, "T", "", None, [f("a.txt", "A")])
    assert 7 not in repo.locked


def test_a_storage_failure_has_a_reason(env):
    from irb_watch.services.errors import UnavailableError

    service, _, storage, _ = env

    def down(*_a):
        raise OSError("storage down")

    storage.put = down
    with pytest.raises(UnavailableError, match="could not be read or stored"):
        service.create_document("admin-1", 7, "T", "", None, [f("a.txt", "A")])


def test_a_missing_document_id_is_an_error_not_zero(env):
    from irb_watch.services.errors import UnavailableError

    service, repo, _, _ = env
    real = repo.commit_scan
    repo.commit_scan = lambda *a: (real(*a), {})[1]  # saved, but no ids came back
    with pytest.raises(UnavailableError, match="id could not be read"):
        service.create_document("admin-1", 7, "T", "", None, [f("a.txt", "A")])


def test_scan_selected_records_why_a_manual_source_is_skipped():
    repo = FakeScanRepo()
    repo.add_source(manual_source())
    audit = FakeAudit()
    scans = ScanService(repo, FakeFileStorage(), lambda s: None, audit)
    runner = ScanRunner(lambda: [manual_source()], repo.get_source, scans, audit, schedule_retry=lambda *_: None)
    report = runner.run_all(only=frozenset({7}))
    assert "manual source" in report.skipped[7]
    assert any(e["event_type"] == "scan.skipped" for e in audit.entries)
