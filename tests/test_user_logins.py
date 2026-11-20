"""A user's own R-BAY login: the service rules, the session provider and the API (all offline)."""

import logging

import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import get_user_login_service, get_user_store, get_verifier
from irb_watch.crawlers.credentials import Credentials
from irb_watch.crawlers.errors import (
    LayoutChangedError,
    LoginRejectedError,
    MissingLoginError,
    SiteUnavailableError,
)
from irb_watch.crawlers.rbay_login import EntryRefusedError
from irb_watch.main import app
from irb_watch.services.draft_jobs import InlineRunner
from irb_watch.services.errors import (
    AuditUnavailableError,
    ConflictError,
    NotFoundError,
    TooManyRequestsError,
    ValidationError,
)
from irb_watch.services.user_logins import SITE, UserLogin, UserLoginService
from irb_watch.services.user_rbay import UserRbaySessions
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_user_logins import Clock, FakeUserLoginStore, ManualRunner
from tests.test_admin_users_api import FakeVerifier

PASSWORD = "S3cret-pw-do-not-leak"


class Checker:
    """Stands in for the headless login: records what it was given, fails on demand."""

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.seen: list[Credentials] = []

    def __call__(self, credentials: Credentials) -> None:
        self.seen.append(credentials)
        if self.error:
            raise self.error


def make(runner=None, audit=None):
    store, checker, clock = FakeUserLoginStore(), Checker(), Clock()
    audit = audit or FakeAudit()
    service = UserLoginService(store, audit, checker, runner or InlineRunner(), clock)
    return service, store, checker, clock, audit


# -- saving and testing -----------------------------------------------------------------------


def test_save_tests_the_login_and_the_page_sees_only_a_status():
    service, store, checker, _, audit = make()
    row = service.save("u1", " abc@x.kr ", PASSWORD)
    assert row.has_login and row.status == "ok" and row.login_hint == "ab***"
    assert checker.seen == [Credentials("abc@x.kr", PASSWORD)]  # the ID is trimmed
    assert row.last_ok_at is not None and row.last_error is None
    assert PASSWORD not in repr(row) and "abc@x.kr" not in repr(row)
    assert store.get_credentials("u1", SITE).password == PASSWORD


def test_status_is_testing_until_the_background_test_ends():
    runner = ManualRunner()
    service, *_ = make(runner)
    assert service.save("u1", "abc", PASSWORD).status == "testing"
    with pytest.raises(ConflictError):
        service.save("u1", "abc", "other-pw")  # a test is running: not twice
    with pytest.raises(ConflictError):
        service.test("u1")
    runner.run()
    assert service.status("u1").status == "ok"


def test_a_rejected_password_is_flagged_and_blocks_jobs_until_a_new_one_is_saved():
    service, _, checker, clock, _ = make()
    checker.error = LoginRejectedError("R-BAY login rejected: 비밀번호가 일치하지 않습니다")
    row = service.save("u1", "abc", "wrong-pw")
    assert row.status == "rejected" and "일치하지" in row.last_error
    with pytest.raises(LoginRejectedError):
        service.credentials_for_job("u1")  # no job may send it again
    checker.error = None
    clock.advance(minutes=11)
    assert service.save("u1", "abc", "right-pw").status == "ok"
    assert service.credentials_for_job("u1") == Credentials("abc", "right-pw")


def test_three_refused_passwords_in_a_row_stop_everything_for_a_day():
    """R-BAY locks an account after 5 failed logins, so the server stops at 3 (not at the daily 10)."""
    service, _, checker, clock, _ = make()
    checker.error = LoginRejectedError("R-BAY login rejected: 아이디 비밀번호가 일치 하지 않습니다")
    for _ in range(3):
        assert service.save("u1", "abc", "wrong-pw").status == "rejected"
        clock.advance(minutes=11)
    attempts = len(checker.seen)
    with pytest.raises(TooManyRequestsError) as exc:
        service.save("u1", "abc", "yet-another")
    assert "5" in exc.value.message and "24 hours" in exc.value.message
    assert len(checker.seen) == attempts  # nothing was sent to R-BAY
    clock.advance(hours=24)
    checker.error = None
    assert service.save("u1", "abc", "right-pw").status == "ok"
    assert service.status("u1").reject_count == 0  # a success starts the count again


def test_testing_a_refused_password_again_is_refused_here_not_sent_to_rbay():
    service, _, checker, clock, _ = make()
    checker.error = LoginRejectedError("R-BAY login rejected: bad")
    service.save("u1", "abc", "wrong-pw")
    clock.advance(minutes=11)
    sent = len(checker.seen)
    with pytest.raises(ConflictError):
        service.test("u1")
    assert len(checker.seen) == sent


def test_a_refusal_in_a_job_counts_toward_the_same_limit():
    service, *_ = make()
    service.save("u1", "abc", PASSWORD)
    service.mark_rejected("u1", "R-BAY login rejected: bad")
    row = service.status("u1")
    assert row.status == "rejected" and row.reject_count == 1


@pytest.mark.parametrize("error", [SiteUnavailableError("no answer"), LayoutChangedError("form changed")])
def test_site_trouble_is_failed_not_rejected_and_does_not_block_jobs(error):
    service, _, checker, _, _ = make()
    checker.error = error
    row = service.save("u1", "abc", PASSWORD)
    assert row.status == "failed" and row.last_error
    assert service.credentials_for_job("u1").password == PASSWORD  # nobody blamed the password


def test_an_unexpected_crash_ends_as_failed_with_a_reason_and_no_secret():
    service, _, checker, _, _ = make()
    checker.error = RuntimeError(f"boom {PASSWORD}")
    row = service.save("u1", "abc", PASSWORD)
    assert row.status == "failed" and PASSWORD not in (row.last_error or "")


def test_a_stale_test_never_changes_the_state_of_a_newer_login():
    """Save A, delete, save B: when A's test finishes late its answer is dropped (the token no longer matches)."""
    runner = ManualRunner()
    service, store, checker, clock, _ = make(runner)
    service.save("u1", "abc", "password-A")  # test A queued
    service.delete("u1")
    clock.advance(minutes=11)
    service.save("u1", "abc", "password-B")  # test B queued
    checker.error = LoginRejectedError("R-BAY login rejected: bad")
    runner.queue.pop(0)()  # A finishes late with a refusal
    row = service.status("u1")
    assert row.status == "testing" and row.reject_count == 0  # B's state is untouched
    checker.error = None
    runner.run()
    assert service.status("u1").status == "ok"


def test_two_requests_at_once_cannot_both_start_a_test():
    """Both read 'untested'; the store's one-statement check lets only the first write through."""

    class StaleReads(FakeUserLoginStore):
        def get(self, user_id, site):
            row = super().get(user_id, site)
            return row if getattr(self, "stale", None) is None else self.stale

    runner = ManualRunner()
    store, checker, clock, audit = StaleReads(), Checker(), Clock(), FakeAudit()
    service = UserLoginService(store, audit, checker, runner, clock)
    service.save("u1", "abc", PASSWORD)  # now 'testing'
    store.stale = UserLogin("u1", has_login=True, status="untested")  # the second request read before that
    with pytest.raises(ConflictError):
        service.test("u1")
    clock.advance(minutes=11)
    with pytest.raises(ConflictError):
        service.save("u1", "abc", "other-pw")
    assert len(runner.queue) == 1  # one R-BAY login, not three
    assert store.get_credentials("u1", SITE).password == PASSWORD


def test_two_refusals_at_the_same_time_both_count():
    service, store, *_ = make()
    service.save("u1", "abc", PASSWORD)
    service.mark_rejected("u1", "R-BAY login rejected: bad")
    service.mark_rejected("u1", "R-BAY login rejected: bad")
    assert service.status("u1").reject_count == 2  # counted by the store, not by a stale read


def test_a_login_missing_when_the_queued_test_runs_ends_untested():
    runner = ManualRunner()
    service, *_ = make(runner)
    service.save("u1", "abc", PASSWORD)
    service.delete("u1")  # deleted while the test waits: its result must not bring the login back
    runner.run()
    row = service.status("u1")
    assert not row.has_login and row.status == "untested"


# -- limits ----------------------------------------------------------------------------------


def test_one_test_per_ten_minutes_and_ten_a_day():
    service, _, _, clock, _ = make()
    service.save("u1", "abc", PASSWORD)  # test 1
    with pytest.raises(TooManyRequestsError):
        service.test("u1")
    for _ in range(9):  # tests 2..10, each 11 minutes apart (all inside 24 hours)
        clock.advance(minutes=11)
        service.test("u1")
    clock.advance(minutes=11)
    with pytest.raises(TooManyRequestsError) as exc:
        service.test("u1")
    assert "10" in exc.value.message
    clock.advance(hours=24)  # a new window
    assert service.test("u1").status == "ok"


def test_saving_and_deleting_again_does_not_reset_the_counters():
    service, *_ = make()
    service.save("u1", "abc", PASSWORD)
    service.delete("u1")
    with pytest.raises(TooManyRequestsError):
        service.save("u1", "abc", PASSWORD)  # still inside the 10 minutes


def test_a_refused_save_stores_nothing():
    service, store, _, _, _ = make()
    service.save("u1", "abc", PASSWORD)
    with pytest.raises(TooManyRequestsError):
        service.save("u1", "abc", "new-pw")
    assert store.get_credentials("u1", SITE).password == PASSWORD


# -- audit, validation, isolation ------------------------------------------------------------


def test_every_action_is_logged_first_without_the_id_or_the_password():
    service, _, _, clock, audit = make()
    service.save("u1", "abc@x.kr", PASSWORD)
    clock.advance(minutes=11)
    service.test("u1")
    service.delete("u1")
    types = [e["event_type"] for e in audit.entries]
    assert types.count("rbay_login.saved") == 1 and "rbay_login.test_started" in types
    assert "rbay_login.deleted" in types and "rbay_login.tested" in types
    assert all(e["category"] == "user" for e in audit.entries)
    blob = repr(audit.entries)
    assert PASSWORD not in blob and "abc@x.kr" not in blob


def test_a_failed_audit_write_means_nothing_happens():
    service, store, *_ = make(audit=FakeAudit(fail_required=True))
    with pytest.raises(AuditUnavailableError):
        service.save("u1", "abc", PASSWORD)
    assert not store.rows
    service2, store2, _, clock, audit2 = make()
    service2.save("u1", "abc", PASSWORD)
    audit2.fail_required = True
    clock.advance(minutes=11)
    with pytest.raises(AuditUnavailableError):
        service2.test("u1")
    with pytest.raises(AuditUnavailableError):
        service2.delete("u1")
    assert store2.get("u1", SITE).has_login


def test_validation_and_missing_login():
    service, *_ = make()
    with pytest.raises(ValidationError):
        service.save("u1", "   ", PASSWORD)
    with pytest.raises(ValidationError):
        service.save("u1", "abc", "x" * 201)
    with pytest.raises(NotFoundError):
        service.test("u1")
    with pytest.raises(NotFoundError):
        service.delete("u1")
    assert service.status("u1").has_login is False


def test_one_user_never_sees_or_uses_another_users_login():
    service, *_ = make()
    service.save("alice", "alice-id", "alice-pw")
    assert not service.status("bob").has_login
    with pytest.raises(MissingLoginError):
        service.credentials_for_job("bob")
    with pytest.raises(NotFoundError):
        service.test("bob")
    with pytest.raises(NotFoundError):
        service.delete("bob")
    assert service.credentials_for_job("alice").login_id == "alice-id"


def test_restart_fails_a_test_that_was_running():
    runner = ManualRunner()
    service, *_ = make(runner)
    service.save("u1", "abc", PASSWORD)
    assert service.fail_unfinished() == 1
    row = service.status("u1")
    assert row.status == "failed" and "restart" in row.last_error


# -- the session provider --------------------------------------------------------------------


class FakeSession:
    def __init__(self) -> None:
        self.user = ""


def provider(service, login_error=None):
    calls = {"logins": [], "logouts": []}

    def login(agency, credentials):
        calls["logins"].append((agency, credentials))
        if login_error:
            raise login_error
        return FakeSession()

    def do_logout(session):
        calls["logouts"].append(session)
        return True

    return UserRbaySessions(service, login, do_logout), calls


def test_a_user_job_logs_in_as_that_user_only_and_always_logs_out():
    service, *_ = make()
    service.save("alice", "alice-id", "alice-pw")
    sessions, calls = provider(service)
    with pytest.raises(RuntimeError), sessions.session("alice", "AG1") as s:
        assert s.user == "user:alice"
        raise RuntimeError("the job failed")
    assert calls["logins"] == [("AG1", Credentials("alice-id", "alice-pw"))]
    assert len(calls["logouts"]) == 1  # logged out although the job raised


def test_a_user_job_without_a_saved_login_never_falls_back_to_anyone_elses():
    service, *_ = make()
    service.save("alice", "alice-id", "alice-pw")
    sessions, calls = provider(service)
    with pytest.raises(MissingLoginError), sessions.session("bob", "AG1"):
        pass
    assert calls["logins"] == []  # nothing was sent to R-BAY


def test_a_refused_password_in_a_job_is_remembered_but_a_hospital_refusal_is_not():
    service, *_ = make()
    service.save("alice", "alice-id", "alice-pw")
    sessions, _ = provider(service, EntryRefusedError("no 입장하기 for this hospital"))
    with pytest.raises(EntryRefusedError), sessions.session("alice", "AG1"):
        pass
    assert service.status("alice").status == "ok"  # the password was fine
    sessions, _ = provider(service, LoginRejectedError("R-BAY login rejected: bad password"))
    with pytest.raises(LoginRejectedError), sessions.session("alice", "AG1"):
        pass
    row = service.status("alice")
    assert row.status == "rejected" and "bad password" in row.last_error
    with pytest.raises(LoginRejectedError), sessions.session("alice", "AG1"):
        pass  # and the next job does not even try


# -- the API ---------------------------------------------------------------------------------


@pytest.fixture
def api():
    users = FakeStore()
    users.add("u1", "one@example.com")
    users.add("u2", "two@example.com")
    service, store, checker, clock, audit = make()
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: users
    app.dependency_overrides[get_user_login_service] = lambda: service
    yield TestClient(app), service, checker, clock
    app.dependency_overrides.clear()


U1 = {"Authorization": "Bearer tok-u1"}
U2 = {"Authorization": "Bearer tok-u2"}


def test_api_needs_a_signed_in_user(api):
    client, *_ = api
    assert client.get("/me/rbay-login").status_code == 401
    assert client.put("/me/rbay-login", json={"login_id": "a", "password": "b"}).status_code == 401


def test_api_save_answers_202_with_a_status_and_never_the_password(api, caplog):
    client, *_ = api
    with caplog.at_level(logging.DEBUG):
        r = client.put("/me/rbay-login", headers=U1, json={"login_id": "abc", "password": PASSWORD})
        g = client.get("/me/rbay-login", headers=U1)
    assert r.status_code == 202 and r.json()["has_login"] is True and r.json()["login_hint"] == "ab***"
    assert g.json()["status"] == "ok"
    assert PASSWORD not in r.text and PASSWORD not in g.text and PASSWORD not in caplog.text
    assert set(r.json()) == {"has_login", "login_hint", "status", "last_error", "last_tested_at", "last_ok_at"}


def test_api_users_are_isolated(api):
    client, *_ = api
    client.put("/me/rbay-login", headers=U1, json={"login_id": "abc", "password": PASSWORD})
    assert client.get("/me/rbay-login", headers=U2).json()["has_login"] is False
    assert client.post("/me/rbay-login/test", headers=U2).status_code == 404
    assert client.delete("/me/rbay-login", headers=U2).status_code == 404
    assert client.get("/me/rbay-login", headers=U1).json()["has_login"] is True


def test_api_a_too_long_password_is_refused_without_echoing_it(api):
    client, *_ = api
    long_password = "p" * 300
    r = client.put("/me/rbay-login", headers=U1, json={"login_id": "abc", "password": long_password})
    assert r.status_code == 422
    assert long_password not in r.text and "input" not in r.json()["detail"][0]
    assert "password" in r.json()["detail"][0]["loc"]


def test_api_test_delete_and_limits(api):
    client, service, checker, clock = api
    client.put("/me/rbay-login", headers=U1, json={"login_id": "abc", "password": PASSWORD})
    assert client.post("/me/rbay-login/test", headers=U1).status_code == 429  # inside 10 minutes
    clock.advance(minutes=11)
    r = client.post("/me/rbay-login/test", headers=U1)
    assert r.status_code == 202 and r.json()["status"] == "ok"
    assert client.delete("/me/rbay-login", headers=U1).status_code == 204
    assert client.get("/me/rbay-login", headers=U1).json() == {
        "has_login": False, "login_hint": None, "status": "untested", "last_error": None,
        "last_tested_at": r.json()["last_tested_at"], "last_ok_at": r.json()["last_ok_at"],
    }
    assert client.delete("/me/rbay-login", headers=U1).status_code == 404


def test_a_store_that_cannot_work_answers_503_with_the_reason_and_logs_no_save(api):
    client, service, *_ = api
    store = service._store
    audit = service._audit
    store.broken = "no key: CREDENTIALS_KEY is not set"
    for r in (
        client.get("/me/rbay-login", headers=U1),
        client.put("/me/rbay-login", headers=U1, json={"login_id": "abc", "password": PASSWORD}),
        client.post("/me/rbay-login/test", headers=U1),
        client.delete("/me/rbay-login", headers=U1),
    ):
        assert r.status_code == 503 and "CREDENTIALS_KEY" in r.json()["detail"]
        assert PASSWORD not in r.text
    assert not [e for e in audit.entries if e["event_type"] == "rbay_login.saved"]  # nothing was saved: not logged


def test_validation_errors_elsewhere_do_not_echo_values_either(api):
    client, *_ = api
    r = client.put("/me/rbay-login", headers=U1, json={"login_id": "", "password": ""})
    assert r.status_code == 422 and "input" not in r.text
