import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import get_token_login_service, get_user_store, get_verifier
from irb_watch.auth.password_login import (
    BadCredentialsError,
    IssuedToken,
    LoginBusyError,
    LoginUnavailableError,
    SupabasePasswordLogin,
)
from irb_watch.main import app
from irb_watch.services import token_login
from irb_watch.services.errors import (
    ForbiddenError,
    RateLimitedError,
    UnauthorizedError,
    UnavailableError,
    UpstreamError,
    ValidationError,
)
from irb_watch.services.token_login import MAX_FAILURES, FailureLimiter, TokenLoginService
from tests.fakes import FakeAudit, FakeStore
from tests.test_admin_users_api import FakeVerifier

T0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class FakeLogin:
    """Accepts 'a@example.com' / 'right'; anything else is wrong."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.raises: Exception | None = None
        self.user_id = "user-1"

    def sign_in(self, email, password):
        self.calls.append((email, password))
        if self.raises:
            raise self.raises
        if (email, password) != ("a@example.com", "right"):
            raise BadCredentialsError
        return IssuedToken("jwt-123", 3600, self.user_id)


@pytest.fixture
def env():
    users = FakeStore()
    users.add("user-1", "a@example.com")
    users.add("gone", "g@example.com", deleted_at="2026-01-01T00:00:00+00:00")
    audit, clock, login = FakeAudit(), Clock(), FakeLogin()
    limiter = FailureLimiter(clock)
    service = TokenLoginService(login, users, audit, limiter)
    return service, login, audit, clock, limiter


def wrong(service, email="a@example.com", ip="1.1.1.1"):
    with pytest.raises(UnauthorizedError):
        service.issue(email, "nope", ip)


# -- the service ------------------------------------------------------------------------


def test_a_correct_login_returns_the_token_and_writes_an_audit_entry_without_secrets(env):
    service, _, audit, *_ = env
    token = service.issue("  A@Example.com ", "right", "1.1.1.1")
    assert (token.access_token, token.expires_in) == ("jwt-123", 3600)
    entry = audit.entries[-1]
    assert (entry["event_type"], entry["actor_id"]) == ("auth.token_issued", "user-1")
    assert "right" not in str(audit.entries) and "jwt-123" not in str(audit.entries)


def test_wrong_email_and_wrong_password_look_the_same(env):
    service, *_ = env
    with pytest.raises(UnauthorizedError) as a:
        service.issue("a@example.com", "wrong", "1.1.1.1")
    with pytest.raises(UnauthorizedError) as b:
        service.issue("nobody@example.com", "wrong", "1.1.1.1")
    assert a.value.message == b.value.message == "email or password is incorrect"


def test_failures_are_not_logged_in_the_audit_log(env):
    service, _, audit, *_ = env
    wrong(service)
    assert audit.entries == []


def test_the_sixth_attempt_for_an_email_is_refused_even_with_the_right_password(env):
    service, login, *_ = env
    for i in range(MAX_FAILURES):
        wrong(service, ip=f"9.9.9.{i}")  # different addresses: the email is what is counted
    calls = len(login.calls)
    with pytest.raises(RateLimitedError) as exc:
        service.issue("a@example.com", "right", "8.8.8.8")
    assert exc.value.status_code == 429 and int(exc.value.headers["Retry-After"]) > 0
    assert len(login.calls) == calls  # the login service was not even asked


def test_the_sixth_attempt_from_an_address_is_refused_whatever_the_email(env):
    service, *_ = env
    for i in range(MAX_FAILURES):
        wrong(service, email=f"x{i}@example.com", ip="7.7.7.7")
    with pytest.raises(RateLimitedError):
        service.issue("a@example.com", "right", "7.7.7.7")
    assert service.issue("a@example.com", "right", "6.6.6.6").access_token == "jwt-123"


def test_the_block_ends_after_15_minutes(env):
    service, _, _, clock, _ = env
    for _ in range(MAX_FAILURES):
        wrong(service)
    with pytest.raises(RateLimitedError):
        service.issue("a@example.com", "right", "1.1.1.1")
    clock.advance(minutes=14)
    with pytest.raises(RateLimitedError):
        service.issue("a@example.com", "right", "1.1.1.1")
    clock.advance(minutes=2)
    assert service.issue("a@example.com", "right", "1.1.1.1").access_token == "jwt-123"


def test_a_success_clears_the_emails_count_but_not_the_addresss(env):
    service, *_ = env
    # The email's count: 4 failures, a success clears them, 4 more still leave a try.
    for i in range(MAX_FAILURES - 1):
        wrong(service, ip=f"2.2.2.{i}")
    service.issue("a@example.com", "right", "5.5.5.5")
    for i in range(MAX_FAILURES - 1):
        wrong(service, ip=f"3.3.3.{i}")
    assert service.issue("a@example.com", "right", "5.5.5.6").access_token == "jwt-123"

    # The address's count is not cleared by a success from that address.
    for _ in range(MAX_FAILURES - 1):
        wrong(service, email="other@example.com", ip="4.4.4.4")
    service.issue("a@example.com", "right", "4.4.4.4")
    wrong(service, email="other@example.com", ip="4.4.4.4")  # the fifth failure for the address
    with pytest.raises(RateLimitedError):
        service.issue("a@example.com", "right", "4.4.4.4")


def test_an_unusable_input_is_refused_before_anything_is_counted(env):
    service, login, *_ = env
    for email, password in [("", "x"), ("a@example.com", ""), ("a" * 400, "x"), ("a@b.c", "p" * 201)]:
        with pytest.raises(ValidationError):
            service.issue(email, password, "1.1.1.1")
    assert login.calls == []


def test_a_deleted_account_gets_no_token_even_with_the_right_password(env):
    service, login, audit, *_ = env
    login.user_id = "gone"
    with pytest.raises(ForbiddenError):
        service.issue("a@example.com", "right", "1.1.1.1")
    assert audit.entries == []


def test_the_login_service_being_busy_or_down_gets_its_own_clear_answer(env):
    service, login, _, _, limiter = env
    login.raises = LoginBusyError()
    with pytest.raises(RateLimitedError) as busy:
        service.issue("a@example.com", "right", "1.1.1.1")
    assert busy.value.headers["Retry-After"] == "60"
    login.raises = LoginUnavailableError("HTTP 500")
    with pytest.raises(UpstreamError):
        service.issue("a@example.com", "right", "1.1.1.1")
    assert limiter.wait_seconds("email:a@example.com") == 0  # not the user's fault, not counted


def test_without_a_publishable_key_the_answer_names_the_setting():
    service = TokenLoginService(None, FakeStore(), FakeAudit(), FailureLimiter())
    with pytest.raises(UnavailableError, match="SUPABASE_PUBLISHABLE_KEY"):
        service.issue("a@example.com", "x", "1.1.1.1")


def test_the_limiter_never_grows_without_bound(monkeypatch):
    monkeypatch.setattr(token_login, "MAX_TRACKED_KEYS", 3)
    limiter = FailureLimiter(Clock())
    for i in range(10):
        limiter.fail(f"k{i}")
    assert len(limiter._failures) <= 3


# -- the call to the login service ------------------------------------------------------


def login_with(handler):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return SupabasePasswordLogin("https://proj.example.co/", "pub-key", http=http)


def test_the_request_to_the_login_service():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["apikey"] = request.headers["apikey"]
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"access_token": "jwt", "expires_in": 3500, "user": {"id": "u-1"}}
        )

    token = login_with(handler).sign_in("a@example.com", "pw")
    assert token == IssuedToken("jwt", 3500, "u-1")
    assert seen["url"] == "https://proj.example.co/auth/v1/token?grant_type=password"
    assert seen["apikey"] == "pub-key" and seen["auth"] is None
    assert seen["body"] == {"email": "a@example.com", "password": "pw"}


@pytest.mark.parametrize(
    "status,error",
    [
        (400, BadCredentialsError),
        (401, BadCredentialsError),
        (422, BadCredentialsError),
        (429, LoginBusyError),
        (500, LoginUnavailableError),
        (503, LoginUnavailableError),
    ],
)
def test_login_service_statuses_map_to_clear_errors(status, error):
    with pytest.raises(error):
        login_with(lambda r: httpx.Response(status, json={"msg": "x"})).sign_in("a@b.c", "pw")


def test_a_garbled_answer_or_a_network_failure_is_unavailable_and_never_shows_the_password():
    with pytest.raises(LoginUnavailableError):
        login_with(lambda r: httpx.Response(200, json={"nope": 1})).sign_in("a@b.c", "s3cret")

    def down(request):
        raise httpx.ConnectError("boom s3cret", request=request)

    with pytest.raises(LoginUnavailableError) as exc:
        login_with(down).sign_in("a@b.c", "s3cret")
    assert "s3cret" not in str(exc.value)


# -- the endpoint ------------------------------------------------------------------------


@pytest.fixture
def api(env):
    service, login, audit, clock, limiter = env
    users = FakeStore()
    users.add("user-1", "a@example.com")
    app.dependency_overrides[get_token_login_service] = lambda: service
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: users
    yield TestClient(app), login
    app.dependency_overrides.clear()


FORM = {"content-type": "application/x-www-form-urlencoded"}


def test_the_form_login_returns_a_bearer_token_and_forbids_caching(api):
    client, _ = api
    res = client.post(
        "/auth/token", data={"grant_type": "password", "username": "a@example.com", "password": "right"}
    )
    assert res.status_code == 200
    assert res.json() == {"access_token": "jwt-123", "token_type": "bearer", "expires_in": 3600}
    assert res.headers["cache-control"] == "no-store"


def test_wrong_password_is_401_with_the_bearer_challenge(api):
    client, _ = api
    res = client.post("/auth/token", data={"username": "a@example.com", "password": "x"})
    assert res.status_code == 401 and res.json()["detail"] == "email or password is incorrect"
    assert res.headers["www-authenticate"] == "Bearer"


def test_too_many_failures_is_429_with_retry_after(api):
    client, _ = api
    for _ in range(MAX_FAILURES):
        client.post("/auth/token", data={"username": "a@example.com", "password": "x"})
    res = client.post("/auth/token", data={"username": "a@example.com", "password": "right"})
    assert res.status_code == 429 and int(res.headers["retry-after"]) > 0


def test_the_client_address_comes_from_the_last_forwarded_entry(api):
    client, login = api
    for _ in range(MAX_FAILURES):
        client.post(
            "/auth/token",
            data={"username": "x@example.com", "password": "x"},
            headers={"X-Forwarded-For": "6.6.6.6, 5.5.5.5"},
        )
    blocked = client.post(
        "/auth/token",
        data={"username": "a@example.com", "password": "right"},
        headers={"X-Forwarded-For": "1.2.3.4, 5.5.5.5"},
    )
    assert blocked.status_code == 429  # same last entry = same address, whatever came before


def test_only_a_form_body_of_reasonable_size_is_accepted(api):
    client, _ = api
    assert client.post("/auth/token", json={"username": "a", "password": "b"}).status_code == 422
    big = "username=a&password=" + "x" * 5000
    assert client.post("/auth/token", content=big, headers=FORM).status_code == 422
    assert client.post("/auth/token", content=b"\xff\xfe", headers=FORM).status_code == 422
    assert client.post("/auth/token", content=b"", headers=FORM).status_code == 422


def test_the_docs_describe_an_oauth2_password_flow_so_the_authorize_button_logs_in(api):
    client, _ = api
    spec = client.get("/openapi.json").json()
    schemes = spec["components"]["securitySchemes"]
    flows = next(iter(schemes.values()))["flows"]
    assert flows["password"]["tokenUrl"] == "/auth/token"
    body = spec["paths"]["/auth/token"]["post"]["requestBody"]["content"]
    assert "application/x-www-form-urlencoded" in body


def test_a_token_from_the_login_works_as_the_bearer_for_other_routes(api):
    client, _ = api
    assert client.get("/admin/users").status_code == 401
    ok = client.get("/admin/users", headers={"Authorization": "Bearer tok-user-1"})
    assert ok.status_code == 403  # signed in, but not an admin: the bearer header is understood
