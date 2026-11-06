from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import get_access_service, get_verifier
from irb_watch.crawlers.credentials import Credentials
from irb_watch.crawlers.errors import (
    FetchError,
    LoginExpiredError,
    LoginRejectedError,
)
from irb_watch.main import app
from irb_watch.services.access import AccessService
from irb_watch.services.errors import (
    AuditUnavailableError,
    ForbiddenError,
    NotFoundError,
    TooManyRequestsError,
    UpstreamError,
    ValidationError,
)
from irb_watch.services.source_login import verify_user_access
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_scan import make_source
from tests.test_admin_users_api import FakeVerifier

NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)
PRIVATE = make_source(
    1, requires_login=True, config={"board_key": "2605277031", "login_user": "owner"}
)
PUBLIC = make_source(2)


class FakeAccessStore:
    def __init__(self):
        self.sources = {1: PRIVATE, 2: PUBLIC}
        self.granted: dict[tuple[str, int], tuple[datetime, datetime]] = {}
        self.attempts = 0

    def get_source(self, source_id):
        return self.sources.get(source_id)

    def grant(self, user_id, source_id, verified_at, expires_at):
        self.granted[(user_id, source_id)] = (verified_at, expires_at)

    def count_attempts_since(self, user_id, since):
        return self.attempts


def make(check=lambda source, creds: None, **kw):
    users = FakeStore()
    users.add("user-1", "u@example.com")
    users.add("gone", "g@example.com", deleted_at="2026-01-01T00:00:00+00:00")
    store, audit = FakeAccessStore(), FakeAudit()
    seen = []

    def spying_check(source, creds):
        seen.append(creds)
        return check(source, creds)

    service = AccessService(users, store, audit, spying_check, clock=lambda: NOW, **kw)
    return service, store, audit, seen


def events(audit):
    return [e["event_type"] for e in audit.entries]


def test_success_grants_30_days_and_stores_no_password():
    service, store, audit, seen = make()
    expires = service.prove_access("user-1", 1, " my-id ", "pw-123")
    assert expires == NOW + timedelta(days=30)
    assert store.granted[("user-1", 1)] == (NOW, expires)
    assert seen[0] == Credentials("my-id", "pw-123")
    assert events(audit) == ["access.attempted", "access.granted"]
    assert "pw-123" not in str(audit.entries)


def test_wrong_password_is_refused_with_the_reason_and_grants_nothing():
    def reject(source, creds):
        raise LoginRejectedError("login rejected: still on the login page")

    service, store, audit, _ = make(reject)
    with pytest.raises(ForbiddenError, match="login rejected"):
        service.prove_access("user-1", 1, "id", "bad-pw-999")
    assert store.granted == {}
    assert events(audit) == ["access.attempted", "access.denied"]
    assert "bad-pw-999" not in str(audit.entries)


def test_login_that_cannot_open_the_board_grants_nothing():
    def no_board(source, creds):
        raise LoginExpiredError("login required or expired")

    service, store, audit, _ = make(no_board)
    with pytest.raises(ForbiddenError, match="cannot open this board"):
        service.prove_access("user-1", 1, "id", "pw")
    assert store.granted == {}


def test_site_trouble_is_a_502_style_error_not_a_denial():
    def broken(source, creds):
        raise FetchError("HTTP 502")

    service, store, _, _ = make(broken)
    with pytest.raises(UpstreamError, match="502"):
        service.prove_access("user-1", 1, "id", "pw")
    assert store.granted == {}


def test_public_source_needs_no_proof():
    service, _, _, seen = make()
    with pytest.raises(ValidationError, match="public"):
        service.prove_access("user-1", 2, "id", "pw")
    assert seen == []


def test_unknown_source_and_bad_accounts():
    service, _, _, _ = make()
    with pytest.raises(NotFoundError):
        service.prove_access("user-1", 99, "id", "pw")
    with pytest.raises(ForbiddenError):
        service.prove_access("gone", 1, "id", "pw")
    with pytest.raises(ForbiddenError):
        service.prove_access("nobody", 1, "id", "pw")
    with pytest.raises(ValidationError):
        service.prove_access("user-1", 1, "  ", "pw")


def test_if_the_attempt_cannot_be_logged_no_hospital_login_is_tried():
    users = FakeStore()
    users.add("user-1", "u@example.com")
    store, audit, seen = FakeAccessStore(), FakeAudit(fail_required=True), []
    service = AccessService(
        users, store, audit, lambda s, c: seen.append(c), clock=lambda: NOW
    )
    with pytest.raises(AuditUnavailableError):
        service.prove_access("user-1", 1, "id", "pw")
    assert seen == [] and store.granted == {}  # the password was never used


def test_attempt_limit_blocks_before_any_hospital_login():
    service, store, audit, seen = make(attempts_per_hour=5)
    store.attempts = 5
    with pytest.raises(TooManyRequestsError, match="5/hour"):
        service.prove_access("user-1", 1, "id", "pw")
    assert seen == [] and audit.entries == []


# ---- the real check wiring (fake browser login + fake board) ---------------------------


class FakeAdapter:
    def __init__(self, error=None):
        self.error, self.closed, self.checked = error, False, []

    def check_board_access(self, board_key):
        self.checked.append(board_key)
        if self.error:
            raise self.error
        return 8

    def close(self):
        self.closed = True


STATE = {"cookies": [{"name": "SID", "value": "v", "domain": "hrpp.snuh.org", "path": "/"}]}


def test_verify_user_access_logs_in_then_opens_this_board_and_always_closes():
    adapter = FakeAdapter()
    calls = []

    def login(url, creds):
        calls.append((url, creds))
        return STATE

    verify_user_access(
        PRIVATE, Credentials("i", "p"), login=login, make_adapter=lambda cookies: adapter
    )
    assert "returnUrl=" in calls[0][0] and adapter.checked == ["2605277031"]
    assert adapter.closed


def test_verify_user_access_propagates_board_failure_and_closes():
    adapter = FakeAdapter(LoginExpiredError("login page"))
    with pytest.raises(LoginExpiredError):
        verify_user_access(
            PRIVATE,
            Credentials("i", "p"),
            login=lambda url, creds: STATE,
            make_adapter=lambda cookies: adapter,
        )
    assert adapter.closed


# ---- endpoint ---------------------------------------------------------------------------


@pytest.fixture
def client():
    service, store, audit, _ = make()
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_access_service] = lambda: service
    yield TestClient(app), store
    app.dependency_overrides.clear()


USER = {"Authorization": "Bearer tok-user-1"}


def test_endpoint_needs_a_login(client):
    c, _ = client
    assert c.post("/sources/1/access", json={"login_id": "a", "password": "b"}).status_code == 401


def test_endpoint_grants_and_returns_expiry(client):
    c, store = client
    r = c.post("/sources/1/access", json={"login_id": "a", "password": "b"}, headers=USER)
    assert r.status_code == 200
    assert r.json()["expires_at"] == (NOW + timedelta(days=30)).isoformat()
    assert ("user-1", 1) in store.granted


@pytest.mark.parametrize(
    "body", [{"login_id": "a"}, {"login_id": "", "password": "b"}, {"login_id": "a", "password": ""}]
)
def test_endpoint_rejects_incomplete_bodies(client, body):
    c, _ = client
    assert c.post("/sources/1/access", json=body, headers=USER).status_code == 422


def test_endpoint_status_for_public_and_unknown_sources(client):
    c, _ = client
    body = {"login_id": "a", "password": "b"}
    assert c.post("/sources/2/access", json=body, headers=USER).status_code == 422
    assert c.post("/sources/99/access", json=body, headers=USER).status_code == 404
