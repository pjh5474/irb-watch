import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from irb_watch.api.deps import (
    get_audit_sink,
    get_managed_credentials,
    get_user_store,
    get_verifier,
)
from irb_watch.crawlers.credentials import Credentials
from irb_watch.crawlers.errors import CredentialStoreError
from irb_watch.db.credentials import DatabaseCredentialStore
from irb_watch.main import app
from irb_watch.security.secretbox import SecretBox, SecretBoxError
from irb_watch.services.errors import AuditUnavailableError
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_scan import scan_once
from tests.test_admin_users_api import FakeVerifier

KEY = Fernet.generate_key().decode()
GOOD = Credentials("hospital-id", "s3cret-pw")


# ---- the secret box ---------------------------------------------------------------------


def test_secretbox_roundtrip_and_ciphertext_hides_the_text():
    box = SecretBox(KEY)
    token = box.encrypt("hospital-id s3cret-pw")
    assert "s3cret-pw" not in token and box.decrypt(token) == "hospital-id s3cret-pw"


@pytest.mark.parametrize("keys", ["", "  ", " , ", "not-a-key", "short"])
def test_secretbox_refuses_a_missing_or_invalid_key(keys):
    with pytest.raises(SecretBoxError):
        SecretBox(keys)


def test_wrong_key_and_damaged_data_fail_loudly_not_with_garbage():
    token = SecretBox(KEY).encrypt("x")
    with pytest.raises(SecretBoxError, match="cannot be decrypted"):
        SecretBox(Fernet.generate_key().decode()).decrypt(token)
    with pytest.raises(SecretBoxError):
        SecretBox(KEY).decrypt(token[:-4] + "AAAA")


def test_key_rotation_new_key_first_old_key_still_reads_old_data():
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    token = SecretBox(old).encrypt("hello")
    rotated = SecretBox(f"{new},{old}")
    assert rotated.decrypt(token) == "hello"
    assert SecretBox(new).decrypt(rotated.encrypt("again")) == "again"  # new key encrypts


# ---- the database store (fake table) ----------------------------------------------------


class FakeTable:
    """Just enough of a Supabase table for site_credentials."""

    def __init__(self, rows):
        self.rows, self._filters, self._op, self._payload = rows, {}, None, None

    def select(self, *_):
        self._op = "select"
        return self

    def upsert(self, payload, on_conflict=None):
        self._op, self._payload = "upsert", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, column, value):
        self._filters[column] = value
        return self

    def order(self, *_):
        return self

    def limit(self, *_):
        return self

    def execute(self):
        match = [r for r in self.rows if all(r.get(k) == v for k, v in self._filters.items())]
        op, self._filters, self._op = self._op, {}, None
        if op == "upsert":
            key = (self._payload["owner_label"], self._payload["site"])
            self.rows[:] = [r for r in self.rows if (r["owner_label"], r["site"]) != key]
            self.rows.append(dict(self._payload))
            return type("R", (), {"data": [self._payload]})()
        if op == "delete":
            for r in match:
                self.rows.remove(r)
            return type("R", (), {"data": match})()
        return type("R", (), {"data": match})()


class FakeClient:
    def __init__(self):
        self.rows: list[dict] = []

    def table(self, name):
        assert name == "site_credentials"
        return FakeTable(self.rows)


def make_store(keys=KEY):
    client = FakeClient()
    return DatabaseCredentialStore(client, keys), client


def test_set_then_get_returns_the_login_and_the_table_holds_only_ciphertext():
    store, client = make_store()
    store.set("owner", "snuh-1", GOOD, updated_by="admin-1")
    assert store.get("owner", "snuh-1") == GOOD
    stored = str(client.rows)
    assert "s3cret-pw" not in stored and "hospital-id" not in stored
    assert client.rows[0]["updated_by"] == "admin-1"


def test_missing_row_is_none_and_set_replaces_the_old_login():
    store, client = make_store()
    assert store.get("owner", "snuh-1") is None
    store.set("owner", "snuh-1", GOOD)
    store.set("owner", "snuh-1", Credentials("new-id", "new-pw"))
    assert len(client.rows) == 1 and store.get("owner", "snuh-1").login_id == "new-id"


def test_missing_key_is_a_credential_store_error_with_the_reason():
    store, client = make_store("")
    with pytest.raises(CredentialStoreError, match="CREDENTIALS_KEY is not set"):
        store.set("owner", "snuh-1", GOOD)
    assert client.rows == []
    good, client2 = make_store()
    good.set("owner", "snuh-1", GOOD)
    keyless = DatabaseCredentialStore(client2, "")
    with pytest.raises(CredentialStoreError, match="CREDENTIALS_KEY is not set"):
        keyless.get("owner", "snuh-1")


def test_wrong_key_is_a_credential_store_error():
    store, client = make_store()
    store.set("owner", "snuh-1", GOOD)
    other = DatabaseCredentialStore(client, Fernet.generate_key().decode())
    with pytest.raises(CredentialStoreError, match="cannot be decrypted"):
        other.get("owner", "snuh-1")


def test_ciphertext_moved_to_another_row_is_refused():
    store, client = make_store()
    store.set("owner", "snuh-1", GOOD)
    client.rows[0]["site"] = "snuh-2"  # someone copies the row to another site
    with pytest.raises(CredentialStoreError, match="do not belong to this row"):
        store.get("owner", "snuh-2")


def test_a_database_failure_is_a_credential_store_error_without_details():
    class Broken:
        def table(self, name):
            raise RuntimeError("connection string with secret")

    with pytest.raises(CredentialStoreError, match="RuntimeError") as info:
        DatabaseCredentialStore(Broken(), KEY).get("owner", "snuh-1")
    assert "secret" not in str(info.value)


def test_list_returns_labels_and_dates_only_and_delete_removes():
    store, client = make_store()
    client.rows.append(
        {"owner_label": "owner", "site": "snuh-1", "ciphertext": "x", "updated_at": "2026-11-06"}
    )
    entries = store.list_entries()
    assert [(e.owner_label, e.site, e.updated_at) for e in entries] == [
        ("owner", "snuh-1", "2026-11-06")
    ]
    assert store.delete("owner", "snuh-1") is True and store.delete("owner", "snuh-1") is False


# ---- a scan with unreadable stored credentials -----------------------------------------------


def test_unreadable_credentials_make_the_source_needs_review_without_a_pause():
    def factory(source):
        raise CredentialStoreError("CREDENTIALS_KEY is not set")

    result, repo = scan_once(factory=factory)
    assert result.status == "needs_review" and "CREDENTIALS_KEY" in result.error
    assert repo.get_source(1).scans_paused_reason is None  # nothing was sent to the site


# ---- admin API ---------------------------------------------------------------------------------


class RecordingStore:
    def __init__(self):
        self.saved, self.deleted = [], []
        self.entries = []

    def set(self, user, site, credentials, updated_by=None):
        self.saved.append((user, site, credentials, updated_by))

    def list_entries(self):
        return self.entries

    def delete(self, user, site):
        self.deleted.append((user, site))
        return site != "missing"


@pytest.fixture
def api():
    users = FakeStore()
    users.add("admin-1", "boss@example.com", role="admin")
    users.add("user-1", "plain@example.com")
    store, audit = RecordingStore(), FakeAudit()
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: users
    app.dependency_overrides[get_managed_credentials] = lambda: store
    app.dependency_overrides[get_audit_sink] = lambda: audit
    yield TestClient(app), store, audit
    app.dependency_overrides.clear()


ADMIN = {"Authorization": "Bearer tok-admin-1"}
BODY = {"login_id": " hospital-id ", "password": "s3cret-pw"}


def test_credentials_endpoints_are_admin_only(api):
    client, store, _ = api
    user = {"Authorization": "Bearer tok-user-1"}
    assert client.put("/admin/credentials/owner/snuh-1", json=BODY).status_code == 401
    assert client.put("/admin/credentials/owner/snuh-1", json=BODY, headers=user).status_code == 403
    assert client.get("/admin/credentials", headers=user).status_code == 403
    assert client.delete("/admin/credentials/owner/snuh-1", headers=user).status_code == 403
    assert store.saved == [] and store.deleted == []


def test_save_stores_the_login_logs_the_change_and_never_echoes_the_password(api):
    client, store, audit = api
    r = client.put("/admin/credentials/owner/snuh-1", json=BODY, headers=ADMIN)
    assert r.status_code == 200 and r.json() == {
        "owner_label": "owner",
        "site": "snuh-1",
        "updated_at": None,
    }
    assert "s3cret-pw" not in r.text and "hospital-id" not in r.text
    assert store.saved == [("owner", "snuh-1", Credentials("hospital-id", "s3cret-pw"), "admin-1")]
    assert audit.entries[0]["event_type"] == "credentials.saved"
    assert "s3cret-pw" not in str(audit.entries) and "hospital-id" not in str(audit.entries)


def test_if_the_change_cannot_be_logged_nothing_is_saved():
    users = FakeStore()
    users.add("admin-1", "boss@example.com", role="admin")
    store = RecordingStore()
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: users
    app.dependency_overrides[get_managed_credentials] = lambda: store
    app.dependency_overrides[get_audit_sink] = lambda: FakeAudit(fail_required=True)
    try:
        r = TestClient(app).put("/admin/credentials/owner/snuh-1", json=BODY, headers=ADMIN)
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == AuditUnavailableError.status_code and store.saved == []


@pytest.mark.parametrize("path", ["bad%20label/snuh-1", "owner/..%2Fx", "owner/a%2Fb"])
def test_labels_are_validated(api, path):
    client, store, _ = api
    r = client.put(f"/admin/credentials/{path}", json=BODY, headers=ADMIN)
    assert r.status_code in (404, 422) and store.saved == []


@pytest.mark.parametrize(
    "body", [{"login_id": "a"}, {"login_id": "", "password": "b"}, {"login_id": "a", "password": ""}]
)
def test_incomplete_bodies_are_rejected(api, body):
    client, store, _ = api
    assert client.put("/admin/credentials/owner/snuh-1", json=body, headers=ADMIN).status_code == 422
    assert store.saved == []


def test_list_shows_labels_only(api):
    from irb_watch.db.credentials import CredentialEntry

    client, store, _ = api
    store.entries = [CredentialEntry("owner", "snuh-1", "2026-11-06T00:00:00+00:00")]
    r = client.get("/admin/credentials", headers=ADMIN)
    assert r.json() == [
        {"owner_label": "owner", "site": "snuh-1", "updated_at": "2026-11-06T00:00:00+00:00"}
    ]


def test_delete_logs_then_removes_and_reports_a_missing_entry(api):
    client, store, audit = api
    assert client.delete("/admin/credentials/owner/snuh-1", headers=ADMIN).status_code == 204
    assert audit.entries[-1]["event_type"] == "credentials.deleted"
    assert client.delete("/admin/credentials/owner/missing", headers=ADMIN).status_code == 404


def test_api_needs_the_database_store(monkeypatch):
    from irb_watch.api import deps
    from irb_watch.services.errors import ConflictError

    class Settings:
        credential_store = "keyring"
        credentials_key = ""

    monkeypatch.setattr(deps, "get_settings", lambda: Settings())
    with pytest.raises(ConflictError, match="CREDENTIAL_STORE=database"):
        deps.get_managed_credentials()
