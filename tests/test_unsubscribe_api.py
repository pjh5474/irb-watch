from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from irb_watch.api import unsubscribe as unsubscribe_module
from irb_watch.api.deps import get_audit_sink, get_digest_store
from irb_watch.mail.unsubscribe import make_token
from irb_watch.main import app
from tests.fakes import FakeAudit
from tests.fakes_mail import FakeDigestStore

KEY = "k" * 64
USER = "0b3a4c6e-1111-4222-8333-944455556666"
OTHER = "1c4b5d7f-2222-4333-9444-a55566667777"


@pytest.fixture
def env(monkeypatch):
    store, audit = FakeDigestStore(), FakeAudit()
    monkeypatch.setattr(unsubscribe_module, "get_settings", lambda: SimpleNamespace(unsubscribe_key=KEY))
    app.dependency_overrides[get_digest_store] = lambda: store
    app.dependency_overrides[get_audit_sink] = lambda: audit
    yield TestClient(app), store, audit
    app.dependency_overrides.clear()


def test_post_with_a_valid_token_switches_that_users_digest_off(env):
    client, store, audit = env
    res = client.post(f"/unsubscribe?token={make_token(USER, KEY)}")
    assert res.status_code == 200 and res.json() == {"status": "unsubscribed"}
    assert store.enabled == {USER: False}
    assert audit.entries[-1]["event_type"] == "digest.unsubscribed" and audit.entries[-1]["actor_id"] == USER


def test_the_one_click_post_mail_apps_send_works_with_its_form_body(env):
    client, store, _ = env
    res = client.post(
        f"/unsubscribe?token={make_token(USER, KEY)}",
        content="List-Unsubscribe=One-Click",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert res.status_code == 200 and store.enabled == {USER: False}


def test_get_changes_nothing_so_a_link_scanner_cannot_unsubscribe_anyone(env):
    client, store, audit = env
    res = client.get(f"/unsubscribe?token={make_token(USER, KEY)}")
    assert res.status_code == 200 and res.json() == {"valid": True}
    assert store.enabled == {} and audit.entries == []


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("token", ["", "garbage", "x" * 300])
def test_an_invalid_token_is_a_400_with_no_detail_and_changes_nothing(env, method, token):
    client, store, _ = env
    res = getattr(client, method)("/unsubscribe", params={"token": token})
    assert res.status_code == 400 and "this unsubscribe link is not valid" in res.json()["detail"]
    assert store.enabled == {}


def test_a_token_for_another_key_or_another_user_does_nothing(env):
    client, store, _ = env
    assert client.post(f"/unsubscribe?token={make_token(USER, 'z' * 64)}").status_code == 400
    signature = make_token(USER, KEY).rpartition(".")[2]
    assert client.post(f"/unsubscribe?token={OTHER}.{signature}").status_code == 400
    assert store.enabled == {}


def test_a_missing_token_is_refused(env):
    client, store, _ = env
    assert client.post("/unsubscribe").status_code == 422 and store.enabled == {}


def test_without_a_server_key_every_link_is_invalid(env, monkeypatch):
    client, store, _ = env
    monkeypatch.setattr(unsubscribe_module, "get_settings", lambda: SimpleNamespace(unsubscribe_key=""))
    assert client.post(f"/unsubscribe?token={make_token(USER, KEY)}").status_code == 400
    assert store.enabled == {}
