"""Organizations: admin rules, monthly statistics and the API (offline, fakes only)."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import (
    get_organization_service,
    get_user_service,
    get_user_store,
    get_verifier,
)
from irb_watch.core import org_stats as s
from irb_watch.core.weekly import UserInfo
from irb_watch.main import app
from irb_watch.services.errors import (
    AuditUnavailableError,
    ConflictError,
    NotFoundError,
    UpstreamError,
    ValidationError,
)
from irb_watch.services.organizations import DEFAULT_ORGANIZATION_ID as DEFAULT
from irb_watch.services.organizations import OrganizationService
from irb_watch.services.users import UserService
from tests.fakes import FakeAudit, FakeAuth, FakeStore
from tests.fakes_orgs import FakeOrgStore
from tests.test_admin_users_api import FakeVerifier

NOW = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)


def make(fail_required: bool = False):
    users = FakeStore()
    users.add("admin-1", "boss@example.com", role="admin")
    users.add("user-1", "a@example.com")
    users.add("user-2", "b@example.com")
    store = FakeOrgStore(users)
    audit = FakeAudit(fail_required=fail_required)
    return OrganizationService(store, audit, clock=lambda: NOW), store, audit


# -- month window ------------------------------------------------------------------------------


def test_month_window_is_the_calendar_month_in_seoul():
    w = s.month_window("2026-11", "Asia/Seoul", NOW)
    assert w.start.isoformat() == "2026-11-01T00:00:00+09:00"
    assert w.end.isoformat() == "2026-12-01T00:00:00+09:00"
    december = s.month_window("2025-12", "Asia/Seoul", NOW)
    assert december.end.isoformat() == "2026-01-01T00:00:00+09:00"


@pytest.mark.parametrize("bad", ["2026-13", "2026-1", "26-11", "2026/11", "abcd-ef", "1999-01"])
def test_month_window_refuses_bad_text(bad):
    with pytest.raises(s.MonthError):
        s.month_window(bad, "Asia/Seoul", NOW)


def test_month_window_refuses_a_future_month():
    with pytest.raises(s.MonthError, match="not started"):
        s.month_window("2026-12", "Asia/Seoul", NOW)


# -- management --------------------------------------------------------------------------------


def test_create_rename_and_name_rules():
    service, _, audit = make()
    org = service.create("admin-1", "  Acme   CRO ")
    assert org.name == "Acme CRO" and audit.entries[-1]["event_type"] == "organization.created"
    with pytest.raises(ConflictError):
        service.create("admin-1", "acme cro")
    with pytest.raises(ValidationError):
        service.create("admin-1", "   ")
    with pytest.raises(ValidationError):
        service.create("admin-1", "x" * 201)
    renamed = service.rename("admin-1", org.id, "Acme")
    assert renamed.name == "Acme" and audit.entries[-1]["payload"]["from"] == "Acme CRO"
    with pytest.raises(ConflictError):
        service.rename("admin-1", org.id, "default")


def test_list_puts_default_first_and_hides_deleted():
    service, store, _ = make()
    store.add_org("Beta")
    store.add_org("Alpha")
    store.add_org("Gone", deleted=True)
    names = [o.name for o in service.list_organizations()]
    assert names == ["Default", "Alpha", "Beta"]
    assert "Gone" in [o.name for o in service.list_organizations(include_deleted=True)]


def test_delete_rules():
    service, store, audit = make()
    with pytest.raises(ConflictError, match="Default"):
        service.delete("admin-1", DEFAULT)
    org = store.add_org("Acme")
    store.join("user-1", org)
    with pytest.raises(ConflictError, match="move its 1 user"):
        service.delete("admin-1", org)
    store.join("user-1", DEFAULT)
    deleted = service.delete("admin-1", org)
    assert deleted.deleted_at is not None
    assert audit.entries[-1]["event_type"] == "organization.deleted"
    with pytest.raises(ConflictError, match="already deleted"):
        service.delete("admin-1", org)
    with pytest.raises(ConflictError, match="deleted"):
        service.rename("admin-1", org, "New")


def test_deleted_users_also_block_delete():
    service, store, _ = make()
    store.users.add("gone", "gone@example.com", deleted_at="2026-11-01T00:00:00+00:00")
    org = store.add_org("Acme")
    store.join("gone", org)
    with pytest.raises(ConflictError, match="deleted accounts too"):
        service.delete("admin-1", org)


def test_restore_rules():
    service, store, _ = make()
    org = store.add_org("Acme", deleted=True)
    restored = service.restore("admin-1", org)
    assert restored.deleted_at is None
    with pytest.raises(ConflictError, match="not deleted"):
        service.restore("admin-1", org)
    twin = store.add_org("Twin", deleted=True)
    store.add_org("Twin")
    with pytest.raises(ConflictError, match="same name"):
        service.restore("admin-1", twin)


def test_unknown_organization_is_404():
    service, *_ = make()
    with pytest.raises(NotFoundError):
        service.members("nope")


def test_move_user_audits_first_and_refuses_deleted_or_unknown():
    service, store, audit = make()
    org = store.add_org("Acme")
    service.move_user("admin-1", "user-1", org)
    assert store.membership["user-1"] == org
    entry = audit.entries[-1]
    assert entry["event_type"] == "user.organization_changed"
    assert entry["payload"] == {"user_id": "user-1", "from": DEFAULT, "to": org}
    count = len(audit.entries)
    service.move_user("admin-1", "user-1", org)  # same organization: nothing happens
    assert len(audit.entries) == count
    with pytest.raises(NotFoundError):
        service.move_user("admin-1", "ghost", org)
    gone = store.add_org("Gone", deleted=True)
    with pytest.raises(ConflictError):
        service.move_user("admin-1", "user-1", gone)


def test_organization_roles_are_set_by_platform_admins_and_never_travel_with_a_move():
    service, store, audit = make()
    org = store.add_org("Acme")
    service.move_user("admin-1", "user-1", org)
    assert service.set_organization_role("admin-1", "user-1", "admin") == "admin"
    assert store.org_roles["user-1"] == "admin"
    assert audit.entries[-1]["event_type"] == "user.organization_role_changed"
    with pytest.raises(ValidationError):
        service.set_organization_role("admin-1", "user-1", "boss")
    with pytest.raises(NotFoundError):
        service.set_organization_role("admin-1", "nobody", "admin")
    other = store.add_org("Other")
    service.move_user("admin-1", "user-1", other)
    assert store.org_roles["user-1"] == "member"  # an admin role would open the new org's projects


def test_move_is_not_done_without_its_audit_entry():
    service, store, _ = make(fail_required=True)
    org = store.add_org("Acme")
    with pytest.raises(AuditUnavailableError):
        service.move_user("admin-1", "user-1", org)
    assert "user-1" not in store.membership


def test_place_new_user_reports_where_the_user_ended_up():
    service, store, _ = make()
    org = store.add_org("Acme")
    store.fail_move = True
    with pytest.raises(UpstreamError, match="created in the Default organization"):
        service.place_new_user("admin-1", "user-1", org)
    service.place_new_user("admin-1", "user-2", DEFAULT)  # Default: nothing to do


# -- statistics --------------------------------------------------------------------------------


def _accounts():
    nov = datetime(2026, 11, 5, tzinfo=UTC)
    oct_ = datetime(2026, 10, 5, tzinfo=UTC)
    return [
        UserInfo("admin-1", "boss@example.com", oct_, nov),
        UserInfo("user-1", "a@example.com", nov, nov),
        UserInfo("user-2", "b@example.com", oct_, oct_),
    ]


def test_month_stats_count_usage_where_it_happened():
    service, store, _ = make()
    acme = store.add_org("Acme")
    store.add_org("Old", deleted=True)
    store.join("user-1", acme)  # moved to Acme; its earlier usage was stamped Default
    store.account_list = _accounts()
    store.usage_rows = [
        s.UsageRow(DEFAULT, "user-1", "agent.message", 4),
        s.UsageRow(acme, "user-1", "agent.message", 2),
        s.UsageRow(acme, "user-1", "summary.requested", 1),
        s.UsageRow(acme, "user-1", "summary.regenerated", 1),
        s.UsageRow(DEFAULT, "user-2", "docgen.generate", 3),
        s.UsageRow(DEFAULT, "user-2", "file.download_link", 5),
        s.UsageRow(DEFAULT, "user-2", "unknown.event", 9),
    ]
    stats = service.month_stats("2026-11")
    by_name = {r.organization.name: r for r in stats.organizations}
    assert set(by_name) == {"Default", "Acme"}  # a deleted one without usage is left out
    assert by_name["Acme"].counts["chat_messages"] == 2
    assert by_name["Acme"].counts["summaries"] == 2
    assert by_name["Default"].counts["chat_messages"] == 4
    assert by_name["Default"].counts["drafts"] == 3 and by_name["Default"].counts["downloads"] == 5
    assert by_name["Acme"].members == 1 and by_name["Default"].members == 2
    assert by_name["Acme"].new_accounts == 1 and by_name["Default"].new_accounts == 0
    assert by_name["Default"].signed_in == 1  # admin-1 in November; user-2 last signed in October
    start, end, types = store.usage_calls[-1]
    assert start.isoformat() == "2026-11-01T00:00:00+09:00" and "agent.message" in types


def test_month_defaults_to_the_current_one():
    service, *_ = make()
    assert service.month_stats().month == "2026-11"


def test_bad_month_is_a_validation_error():
    service, *_ = make()
    with pytest.raises(ValidationError):
        service.month_stats("2027-01")


def test_deleted_organization_with_usage_still_shows():
    service, store, _ = make()
    old = store.add_org("Old", deleted=True)
    store.usage_rows = [s.UsageRow(old, "user-2", "docgen.export", 1)]
    names = {r.organization.name: r for r in service.month_stats("2026-11").organizations}
    assert names["Old"].organization.deleted and names["Old"].counts["exports"] == 1


def test_organization_stats_list_members_and_moved_users():
    service, store, _ = make()
    acme = store.add_org("Acme")
    store.join("user-2", acme)
    store.account_list = _accounts()
    store.usage_rows = [
        s.UsageRow(acme, "user-1", "agent.message", 3),  # user-1 was in Acme, now in Default
        s.UsageRow(DEFAULT, "user-2", "agent.message", 7),  # before user-2 moved: not Acme's
    ]
    stats = service.organization_stats(acme, "2026-11")
    rows = {u.email: u for u in stats.users}
    assert set(rows) == {"a@example.com", "b@example.com"}
    assert rows["a@example.com"].member_now is False and rows["a@example.com"].counts["chat_messages"] == 3
    assert rows["b@example.com"].member_now is True and rows["b@example.com"].counts["chat_messages"] == 0
    assert stats.users[0].email == "a@example.com"  # most usage first
    assert stats.totals.members == 1 and stats.totals.counts["chat_messages"] == 3


# -- API ---------------------------------------------------------------------------------------


@pytest.fixture
def api():
    users = FakeStore()
    users.add("admin-1", "boss@example.com", role="admin")
    users.add("user-1", "plain@example.com")
    audit = FakeAudit()
    store = FakeOrgStore(users)
    orgs = OrganizationService(store, audit, clock=lambda: NOW)
    service = UserService(users, FakeAuth(users), audit, min_password_length=8)
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: users
    app.dependency_overrides[get_user_service] = lambda: service
    app.dependency_overrides[get_organization_service] = lambda: orgs
    yield TestClient(app), store, users, audit
    app.dependency_overrides.clear()


ADMIN = {"Authorization": "Bearer tok-admin-1"}


def test_api_is_admin_only(api):
    client, *_ = api
    user = {"Authorization": "Bearer tok-user-1"}
    assert client.get("/admin/organizations", headers=user).status_code == 403
    assert client.get("/admin/organizations/stats", headers=user).status_code == 403
    assert client.get("/admin/organizations").status_code == 401


def test_api_create_list_rename_delete_restore(api):
    client, *_ = api
    r = client.post("/admin/organizations", headers=ADMIN, json={"name": "Acme"})
    assert r.status_code == 201
    org = r.json()
    assert org["is_default"] is False and org["member_count"] == 0
    listed = client.get("/admin/organizations", headers=ADMIN).json()
    assert [o["name"] for o in listed] == ["Default", "Acme"]
    assert listed[0]["is_default"] and listed[0]["member_count"] == 2
    assert client.post("/admin/organizations", headers=ADMIN, json={"name": "acme"}).status_code == 409
    assert client.post("/admin/organizations", headers=ADMIN, json={"name": ""}).status_code == 422
    r = client.patch(f"/admin/organizations/{org['id']}", headers=ADMIN, json={"name": "Acme 2"})
    assert r.status_code == 200 and r.json()["name"] == "Acme 2"
    assert client.delete(f"/admin/organizations/{DEFAULT}", headers=ADMIN).status_code == 409
    r = client.delete(f"/admin/organizations/{org['id']}", headers=ADMIN)
    assert r.status_code == 200 and r.json()["deleted_at"]
    assert len(client.get("/admin/organizations", headers=ADMIN).json()) == 1
    assert len(client.get("/admin/organizations?include_deleted=true", headers=ADMIN).json()) == 2
    r = client.post(f"/admin/organizations/{org['id']}/restore", headers=ADMIN)
    assert r.status_code == 200 and r.json()["deleted_at"] is None
    assert client.delete("/admin/organizations/nope", headers=ADMIN).status_code == 404


def test_api_members_and_move(api):
    client, store, _, audit = api
    acme = store.add_org("Acme")
    r = client.put("/admin/users/user-1/organization", headers=ADMIN, json={"organization_id": acme})
    assert r.status_code == 200
    assert r.json()["organization_id"] == acme and r.json()["organization_name"] == "Acme"
    members = client.get(f"/admin/organizations/{acme}/members", headers=ADMIN).json()
    assert [m["email"] for m in members] == ["plain@example.com"]
    users = client.get("/admin/users", headers=ADMIN).json()["items"]
    assert {u["id"]: u["organization_name"] for u in users} == {
        "admin-1": "Default", "user-1": "Acme"
    }
    r = client.put("/admin/users/ghost/organization", headers=ADMIN, json={"organization_id": acme})
    assert r.status_code == 404


def test_api_create_user_in_an_organization(api):
    client, store, users, _ = api
    acme = store.add_org("Acme")
    body = {"email": "new@example.com", "password": "longenough", "organization_id": acme}
    r = client.post("/admin/users", headers=ADMIN, json=body)
    assert r.status_code == 201 and r.json()["organization_name"] == "Acme"
    r = client.post(
        "/admin/users", headers=ADMIN, json={"email": "d@example.com", "password": "longenough"}
    )
    assert r.status_code == 201 and r.json()["organization_id"] == DEFAULT


def test_api_create_user_checks_the_organization_before_the_account(api):
    client, store, users, _ = api
    gone = store.add_org("Gone", deleted=True)
    body = {"email": "new@example.com", "password": "longenough", "organization_id": gone}
    assert client.post("/admin/users", headers=ADMIN, json=body).status_code == 409
    body["organization_id"] = "nope"
    assert client.post("/admin/users", headers=ADMIN, json=body).status_code == 404
    assert users.get_profile_by_email("new@example.com") is None


def test_api_stats(api):
    client, store, *_ = api
    store.usage_rows = [s.UsageRow(DEFAULT, "user-1", "agent.message", 2)]
    r = client.get("/admin/organizations/stats?month=2026-11", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["month"] == "2026-11"
    assert body["organizations"][0]["counts"]["chat_messages"] == 2
    assert client.get("/admin/organizations/stats?month=2026-1", headers=ADMIN).status_code == 422
    assert client.get("/admin/organizations/stats?month=2030-01", headers=ADMIN).status_code == 422
    r = client.get(f"/admin/organizations/{DEFAULT}/stats", headers=ADMIN)
    assert r.status_code == 200 and r.json()["month"] == "2026-11"
    assert r.json()["users"][0]["counts"]["chat_messages"] == 2
