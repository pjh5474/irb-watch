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
from irb_watch.db.access import SupabaseAccessStore
from irb_watch.main import app
from irb_watch.services.access import AccessService, id_fingerprint, unsettled_attempts
from irb_watch.services.errors import (
    AuditUnavailableError,
    ForbiddenError,
    NotFoundError,
    TooManyRequestsError,
    UnavailableError,
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


KEY = "test-hash-key"


class FakeAccessStore:
    def __init__(self, audit=None):
        self.sources = {1: PRIVATE, 2: PUBLIC}
        self.granted: dict[tuple[str, int], tuple[datetime, datetime]] = {}
        self.attempts = 0
        self.audit = audit  # outcomes are read back from the audit entries, like the real store

    def get_source(self, source_id):
        return self.sources.get(source_id)

    def grant(self, user_id, source_id, verified_at, expires_at):
        self.granted[(user_id, source_id)] = (verified_at, expires_at)

    def count_attempts_since(self, user_id, since):
        return self.attempts

    def id_outcomes_since(self, fingerprint, since):
        return [
            (e["event_type"], e["payload"].get("reason"))
            for e in self.audit.entries
            if e["event_type"].startswith("access.") and e["payload"].get("id_fp") == fingerprint
        ]


def make(check=lambda source, creds: None, **kw):
    users = FakeStore()
    for name in ("user-1", "user-2", "user-3", "user-4"):
        users.add(name, f"{name}@example.com")
    users.add("gone", "g@example.com", deleted_at="2026-01-01T00:00:00+00:00")
    audit = FakeAudit()
    store = FakeAccessStore(audit)
    seen = []

    def spying_check(source, creds):
        seen.append(creds)
        return check(source, creds)

    kw.setdefault("id_hash_key", KEY)
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
    audit, seen = FakeAudit(fail_required=True), []
    store = FakeAccessStore(audit)
    service = AccessService(
        users, store, audit, lambda s, c: seen.append(c), clock=lambda: NOW, id_hash_key=KEY
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


# ---- lockout protection per hospital ID ---------------------------------------------------


def rejecting(source, creds):
    raise LoginRejectedError("login rejected: x")


def test_hospital_id_is_logged_only_as_a_keyed_hash():
    service, _, audit, _ = make()
    service.prove_access("user-1", 1, "Hospital-ID-77", "pw-123")
    fingerprint = id_fingerprint(KEY, "Hospital-ID-77")
    assert [e["payload"]["id_fp"] for e in audit.entries] == [fingerprint, fingerprint]
    assert "Hospital-ID-77" not in str(audit.entries) and "pw-123" not in str(audit.entries)


def test_fingerprint_ignores_case_and_spaces_and_depends_on_the_key():
    assert id_fingerprint("k", "Abc ") == id_fingerprint("k", " abc")
    assert id_fingerprint("k", "abc") != id_fingerprint("other", "abc")
    assert len(id_fingerprint("k", "abc")) == 64


def test_three_failed_logins_for_one_hospital_id_block_it_for_everyone():
    service, _, audit, seen = make(rejecting)
    for user in ("user-1", "user-2", "user-3"):  # three different app users, one hospital ID
        with pytest.raises(ForbiddenError):
            service.prove_access(user, 1, "shared-id", "bad")
    logged = len(audit.entries)
    with pytest.raises(TooManyRequestsError, match="hospital ID"):
        service.prove_access("user-4", 1, "SHARED-ID ", "bad")  # another user, same ID
    assert len(seen) == 3 and len(audit.entries) == logged  # no 4th login, nothing logged
    with pytest.raises(ForbiddenError):  # a different ID is not affected
        service.prove_access("user-4", 1, "other-id", "bad")
    assert len(seen) == 4


def test_successes_and_unjudged_checks_do_not_count_against_the_id():
    results = iter([None, None, None, LoginExpiredError("x"), FetchError("site down"), None])

    def check(source, creds):
        outcome = next(results)
        if outcome:
            raise outcome

    service, _, _, seen = make(check, attempts_per_hour=99)
    for _ in range(6):
        try:
            service.prove_access("user-1", 1, "id-1", "pw")
        except (ForbiddenError, UpstreamError):
            pass
    assert len(seen) == 6  # never blocked


def test_a_check_that_never_logged_its_result_counts_like_a_failure():
    service, _, audit, seen = make()
    fingerprint = id_fingerprint(KEY, "id-1")
    for _ in range(3):  # three attempts still running, or whose result was lost
        audit.record("user", "access.attempted", "user-2", payload={"id_fp": fingerprint})
    with pytest.raises(TooManyRequestsError):
        service.prove_access("user-1", 1, "id-1", "pw")
    assert seen == []


def test_without_the_hash_key_access_checks_refuse_and_try_no_login():
    service, store, audit, seen = make(id_hash_key="")
    with pytest.raises(UnavailableError, match="ACCESS_ID_HASH_KEY"):
        service.prove_access("user-1", 1, "id", "pw")
    assert seen == [] and audit.entries == [] and store.granted == {}


def test_unsettled_attempts_arithmetic():
    attempted, granted = ("access.attempted", None), ("access.granted", None)
    assert unsettled_attempts([]) == 0
    assert unsettled_attempts([attempted, attempted, granted]) == 1
    assert unsettled_attempts([granted]) == 0  # an old attempt outside the window: never negative
    assert unsettled_attempts([attempted, ("access.denied", "login_rejected")]) == 1
    assert unsettled_attempts([attempted, ("access.denied", "check_failed")]) == 0


class QueryLog:
    """Chainable stand-in for a Supabase table query that records the calls."""

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args))
            return self

        return call

    def execute(self):
        return type("Result", (), {"data": self.rows})()


def test_supabase_store_reads_the_hour_of_outcomes_for_one_hashed_id():
    query = QueryLog(
        [
            {"event_type": "access.attempted", "payload": {"id_fp": "f"}},
            {"event_type": "access.denied", "payload": {"id_fp": "f", "reason": "check_failed"}},
            {"event_type": "access.granted", "payload": None},
        ]
    )
    client = type("Client", (), {"table": lambda self, name: query})()
    since = NOW - timedelta(hours=1)
    outcomes = SupabaseAccessStore(client).id_outcomes_since("f", since)
    assert outcomes == [
        ("access.attempted", None),
        ("access.denied", "check_failed"),
        ("access.granted", None),
    ]
    assert ("eq", ("payload->>id_fp", "f")) in query.calls
    assert ("gte", ("created_at", since.isoformat())) in query.calls


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
