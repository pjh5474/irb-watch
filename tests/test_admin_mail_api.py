import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import get_alert_service, get_audit_sink, get_user_store, get_verifier
from irb_watch.main import app
from irb_watch.services.alerts import AlertService
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_mail import FakeAlertStore, FakeMailer
from tests.test_admin_users_api import FakeVerifier


@pytest.fixture
def env():
    users = FakeStore()
    users.add("admin-1", "admin@example.com", role="admin")
    users.add("user-1", "u@example.com")
    users.add("no-mail", None, role="admin")
    mailer, audit = FakeMailer(), FakeAudit()

    def make(enabled=True):
        return AlertService(
            FakeAlertStore(),
            mailer if enabled else None,
            audit,
            list,
            lambda i: None,
            app_url="https://app.example.com" if enabled else "",
        )

    state = {"service": make()}
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: users
    app.dependency_overrides[get_audit_sink] = lambda: audit
    app.dependency_overrides[get_alert_service] = lambda: state["service"]
    yield TestClient(app), mailer, audit, state, make
    app.dependency_overrides.clear()


def auth(user):
    return {"Authorization": f"Bearer tok-{user}"}


def test_the_test_mail_goes_to_the_admins_own_address_only(env):
    client, mailer, audit, *_ = env
    res = client.post("/admin/mail/test", headers=auth("admin-1"))
    assert res.status_code == 200 and res.json() == {"status": "sent"}
    assert mailer.sent[0].to == ("admin@example.com",)
    assert audit.entries[-1]["event_type"] == "mail.test_sent"
    assert "example.com" not in str(audit.entries)


def test_only_admins_may_send_a_test_mail(env):
    client, mailer, *_ = env
    assert client.post("/admin/mail/test").status_code == 401
    assert client.post("/admin/mail/test", headers=auth("user-1")).status_code == 403
    assert mailer.sent == []


def test_not_set_up_is_a_409_with_the_names_of_the_settings(env):
    client, mailer, _, state, make = env
    state["service"] = make(enabled=False)
    res = client.post("/admin/mail/test", headers=auth("admin-1"))
    assert res.status_code == 409 and "RESEND_API_KEY" in res.json()["detail"]
    assert mailer.sent == []


def test_a_refusal_from_the_mail_service_is_a_502_with_its_reason(env):
    client, mailer, audit, *_ = env
    mailer.fail = "the mail service refused the message (HTTP 403: domain is not verified)"
    res = client.post("/admin/mail/test", headers=auth("admin-1"))
    assert res.status_code == 502 and "domain is not verified" in res.json()["detail"]
    assert audit.entries[-1]["event_type"] == "mail.test_failed"


def test_an_admin_without_an_email_address_gets_a_clear_error(env):
    client, mailer, *_ = env
    res = client.post("/admin/mail/test", headers=auth("no-mail"))
    assert res.status_code == 422 and "email" in res.json()["detail"]
    assert mailer.sent == []
