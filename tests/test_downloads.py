from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import get_download_service, get_verifier
from irb_watch.db.downloads import SupabaseUrlSigner
from irb_watch.main import app
from irb_watch.services.downloads import DownloadService, StoredFile
from irb_watch.services.errors import (
    AuditUnavailableError,
    ForbiddenError,
    NotFoundError,
    TooManyRequestsError,
    UpstreamError,
)
from tests.fakes import FakeAudit, FakeStore
from tests.test_admin_users_api import FakeVerifier

NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)
FILE = StoredFile(10, 1, 100, 2, "[공문] 서식.pdf", "sha-abc", "sha-abc")
NOT_STORED = StoredFile(11, 1, 100, 2, "scan.pdf", "sha-def", None)
OTHER_SOURCE = StoredFile(12, 2, 200, 1, "other.pdf", "sha-ghi", "sha-ghi")


class FakeRepo:
    def __init__(self):
        self.files = {f.file_id: f for f in (FILE, NOT_STORED, OTHER_SOURCE)}
        self.readable = {("user-1", 1)}  # (user, source) the user may read
        self.links = 0

    def get_file(self, file_id):
        return self.files.get(file_id)

    def may_read(self, user_id, source_id, now):
        return (user_id, source_id) in self.readable

    def count_links_since(self, user_id, since):
        return self.links


class FakeSigner:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    def sign(self, storage_path, filename, expires_in):
        self.calls.append((storage_path, filename, expires_in))
        if self.error:
            raise self.error
        return f"https://storage.example/{storage_path}?token=t&download={filename}"


def make(**kw):
    users = FakeStore()
    users.add("user-1", "u@example.com")
    users.add("other", "o@example.com")
    users.add("admin-1", "a@example.com", role="admin")
    users.add("gone", "g@example.com", deleted_at="2026-01-01T00:00:00+00:00")
    repo, signer, audit = FakeRepo(), kw.pop("signer", FakeSigner()), FakeAudit()
    service = DownloadService(users, repo, signer, audit, clock=lambda: NOW, **kw)
    return service, repo, signer, audit


def test_reader_gets_a_60_second_link_with_the_original_filename():
    service, _, signer, audit = make()
    link = service.download_link("user-1", 10)
    assert link.expires_at == NOW + timedelta(seconds=60)
    assert link.filename == "[공문] 서식.pdf" and link.url.startswith("https://storage.example/sha-abc")
    assert signer.calls == [("sha-abc", "[공문] 서식.pdf", 60)]
    entry = audit.entries[-1]
    assert entry["event_type"] == "file.download_link" and entry["actor_id"] == "user-1"
    assert entry["source_id"] == 1 and entry["document_id"] == 100
    assert entry["payload"] == {"file_id": 10, "sha256": "sha-abc", "version_no": 2}


def test_user_who_may_not_read_the_source_sees_not_found_and_gets_no_link():
    service, _, signer, audit = make()
    for user, file_id in (("other", 10), ("user-1", 12), ("user-1", 999)):
        with pytest.raises(NotFoundError, match="file not found"):
            service.download_link(user, file_id)
    assert signer.calls == [] and audit.entries == []


def test_members_only_rule_comes_from_the_repo_so_expired_proof_means_not_found():
    service, repo, signer, _ = make()
    repo.readable.clear()  # e.g. proof expired or never given
    with pytest.raises(NotFoundError):
        service.download_link("user-1", 10)
    assert signer.calls == []


def test_admin_may_download_anything_and_is_not_limited():
    service, repo, _, _ = make(daily_limit=1)
    repo.links = 500
    assert service.download_link("admin-1", 12).filename == "other.pdf"


def test_deleted_and_unknown_accounts_are_refused():
    service, _, signer, _ = make()
    for user in ("gone", "nobody"):
        with pytest.raises(ForbiddenError):
            service.download_link(user, 10)
    assert signer.calls == []


def test_file_without_a_stored_original_says_why():
    service, _, signer, _ = make()
    with pytest.raises(NotFoundError, match="was not stored"):
        service.download_link("user-1", 11)
    assert signer.calls == []


def test_daily_limit_blocks_before_signing_and_logging():
    service, repo, signer, audit = make(daily_limit=200)
    repo.links = 200
    with pytest.raises(TooManyRequestsError, match="200"):
        service.download_link("user-1", 10)
    assert signer.calls == [] and audit.entries == []
    repo.links = 199
    service.download_link("user-1", 10)


def test_signing_failure_is_an_error_with_a_reason_and_is_not_logged_as_a_download():
    service, _, _, audit = make(signer=FakeSigner(RuntimeError("storage down")))
    with pytest.raises(UpstreamError, match="RuntimeError"):
        service.download_link("user-1", 10)
    assert audit.entries == []


def test_if_the_log_entry_cannot_be_written_no_link_is_handed_out():
    users = FakeStore()
    users.add("user-1", "u@example.com")
    audit, signer = FakeAudit(fail_required=True), FakeSigner()
    service = DownloadService(users, FakeRepo(), signer, audit, clock=lambda: NOW)
    with pytest.raises(AuditUnavailableError, match="not carried out|audit log"):
        service.download_link("user-1", 10)
    assert audit.entries == []  # nothing logged, and the caller got no URL


def test_admin_downloads_are_logged_fail_closed_too():
    users = FakeStore()
    users.add("admin-1", "a@example.com", role="admin")
    service = DownloadService(
        users, FakeRepo(), FakeSigner(), FakeAudit(fail_required=True), clock=lambda: NOW
    )
    with pytest.raises(AuditUnavailableError):
        service.download_link("admin-1", 10)


def test_custom_ttl_is_used():
    service, _, signer, _ = make(ttl_seconds=30)
    assert service.download_link("user-1", 10).expires_at == NOW + timedelta(seconds=30)
    assert signer.calls[0][2] == 30


# ---- the Supabase signer ---------------------------------------------------------------------


class FakeBucket:
    def __init__(self, result):
        self.result, self.calls = result, []

    def create_signed_url(self, path, expires_in, options):
        self.calls.append((path, expires_in, options))
        return self.result


class FakeStorageClient:
    def __init__(self, result):
        self.bucket = FakeBucket(result)
        self.storage = self

    def from_(self, name):
        assert name == "documents"
        return self.bucket


def test_signer_asks_storage_for_a_download_link_with_the_filename():
    client = FakeStorageClient({"signedURL": "https://x/sign?token=t", "signedUrl": "https://x/sign?token=t"})
    url = SupabaseUrlSigner(client).sign("sha-abc", "a b.pdf", 60)
    assert url == "https://x/sign?token=t"
    assert client.bucket.calls == [("sha-abc", 60, {"download": "a b.pdf"})]


def test_signer_fails_loudly_when_storage_returns_no_url():
    with pytest.raises(RuntimeError, match="no signed URL"):
        SupabaseUrlSigner(FakeStorageClient({"signedURL": None, "signedUrl": None})).sign("p", "f", 60)


# ---- endpoint ------------------------------------------------------------------------------------


@pytest.fixture
def client():
    service, repo, signer, audit = make()
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_download_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_endpoint_needs_a_login(client):
    assert client.post("/files/10/download-url").status_code == 401


def test_endpoint_returns_the_link(client):
    r = client.post("/files/10/download-url", headers={"Authorization": "Bearer tok-user-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "[공문] 서식.pdf" and body["url"].startswith("https://storage.example/")
    assert body["expires_at"] == (NOW + timedelta(seconds=60)).isoformat()


@pytest.mark.parametrize(
    "token,file_id,status",
    [("tok-other", 10, 404), ("tok-user-1", 999, 404), ("tok-user-1", 11, 404), ("tok-gone", 10, 403)],
)
def test_endpoint_status_codes(client, token, file_id, status):
    r = client.post(f"/files/{file_id}/download-url", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == status
