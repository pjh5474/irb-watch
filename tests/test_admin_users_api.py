import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import (
    get_organization_service,
    get_user_service,
    get_user_store,
    get_verifier,
)
from irb_watch.auth.jwt import AuthUnavailableError, AuthUser, InvalidTokenError
from irb_watch.main import app
from irb_watch.services.organizations import OrganizationService
from irb_watch.services.users import UserService
from tests.fakes import FakeAudit, FakeAuth, FakeStore
from tests.fakes_orgs import FakeOrgStore


class FakeVerifier:
    """Token text 'tok-<user id>' is valid for that user."""

    def verify(self, token):
        if token == "unavailable":
            raise AuthUnavailableError("down")
        if not token.startswith("tok-"):
            raise InvalidTokenError("bad")
        return AuthUser(id=token[4:], email=None)


@pytest.fixture
def api():
    store = FakeStore()
    store.add("admin-1", "boss@example.com", role="admin")
    store.add("user-1", "plain@example.com")
    store.add("ex-admin", "ex@example.com", role="admin", deleted_at="2026-01-01T00:00:00+00:00")
    auth = FakeAuth(store)
    audit = FakeAudit()
    service = UserService(store, auth, audit, min_password_length=8)
    orgs = OrganizationService(FakeOrgStore(store), audit)
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: store
    app.dependency_overrides[get_user_service] = lambda: service
    app.dependency_overrides[get_organization_service] = lambda: orgs
    yield TestClient(app), store, auth, audit
    app.dependency_overrides.clear()


def as_admin():
    return {"Authorization": "Bearer tok-admin-1"}


def test_missing_token_is_401(api):
    client, *_ = api
    assert client.get("/admin/users").status_code == 401


def test_invalid_token_is_401(api):
    client, *_ = api
    r = client.get("/admin/users", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


def test_key_outage_is_503(api):
    client, *_ = api
    assert client.get("/admin/users", headers={"Authorization": "Bearer unavailable"}).status_code == 503


def test_regular_user_is_403(api):
    client, *_ = api
    assert client.get("/admin/users", headers={"Authorization": "Bearer tok-user-1"}).status_code == 403


def test_soft_deleted_admin_is_403(api):
    client, *_ = api
    assert client.get("/admin/users", headers={"Authorization": "Bearer tok-ex-admin"}).status_code == 403


def test_token_without_profile_is_403(api):
    client, *_ = api
    assert client.get("/admin/users", headers={"Authorization": "Bearer tok-ghost"}).status_code == 403


def test_demoted_admin_loses_access_immediately(api):
    client, store, *_ = api
    assert client.get("/admin/users", headers=as_admin()).status_code == 200
    store.set_role("admin-1", "user")
    assert client.get("/admin/users", headers=as_admin()).status_code == 403


def test_create_user(api):
    client, store, auth, audit = api
    r = client.post(
        "/admin/users",
        headers=as_admin(),
        json={"email": "New@Example.com", "password": "temporary-pass", "display_name": "New"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "new@example.com" and body["role"] == "user"
    assert "password" not in body and "temporary-pass" not in r.text
    assert audit.entries[-1]["actor_id"] == "admin-1"


def test_create_user_short_password_is_422_and_never_echoed(api):
    client, *_ = api
    r = client.post(
        "/admin/users", headers=as_admin(), json={"email": "a@example.com", "password": "short"}
    )
    assert r.status_code == 422
    assert "short" not in r.text.replace("shorter", "")


def test_create_user_bad_email_is_422(api):
    client, *_ = api
    r = client.post(
        "/admin/users", headers=as_admin(), json={"email": "nope", "password": "longenough"}
    )
    assert r.status_code == 422
    assert "longenough" not in r.text


def test_create_duplicate_is_409(api):
    client, *_ = api
    r = client.post(
        "/admin/users",
        headers=as_admin(),
        json={"email": "plain@example.com", "password": "longenough"},
    )
    assert r.status_code == 409


def test_list_hides_deleted_by_default(api):
    client, *_ = api
    body = client.get("/admin/users", headers=as_admin()).json()
    assert body["total"] == 2
    assert {u["id"] for u in body["items"]} == {"admin-1", "user-1"}


def test_list_can_include_deleted_and_paginate(api):
    client, *_ = api
    body = client.get("/admin/users?include_deleted=true&limit=2&offset=0", headers=as_admin()).json()
    assert body["total"] == 3 and len(body["items"]) == 2


def test_list_limit_is_bounded(api):
    client, *_ = api
    assert client.get("/admin/users?limit=0", headers=as_admin()).status_code == 422
    assert client.get("/admin/users?limit=1000", headers=as_admin()).status_code == 422


def test_change_role(api):
    client, *_ = api
    r = client.patch("/admin/users/user-1/role", headers=as_admin(), json={"role": "admin"})
    assert r.status_code == 200 and r.json()["role"] == "admin"


def test_invalid_role_is_422(api):
    client, *_ = api
    r = client.patch("/admin/users/user-1/role", headers=as_admin(), json={"role": "root"})
    assert r.status_code == 422


def test_cannot_demote_yourself_via_api(api):
    client, store, *_ = api
    store.add("admin-2", "b@example.com", role="admin")
    r = client.patch("/admin/users/admin-1/role", headers=as_admin(), json={"role": "user"})
    assert r.status_code == 409


def test_delete_then_restore(api):
    client, store, auth, _ = api
    r = client.delete("/admin/users/user-1", headers=as_admin())
    assert r.status_code == 200 and r.json()["deleted_at"] is not None
    assert "user-1" in auth.banned
    r = client.post("/admin/users/user-1/restore", headers=as_admin())
    assert r.status_code == 200 and r.json()["deleted_at"] is None


def test_delete_unknown_is_404(api):
    client, *_ = api
    assert client.delete("/admin/users/nope", headers=as_admin()).status_code == 404


def test_cors_allows_configured_origin_only(api):
    client, *_ = api
    ok = client.options(
        "/admin/users",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
    )
    bad = client.options(
        "/admin/users",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert "access-control-allow-origin" not in bad.headers
