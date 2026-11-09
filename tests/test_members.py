import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import get_member_service, get_user_store, get_verifier
from irb_watch.main import app
from irb_watch.services.errors import AuditUnavailableError, NotFoundError, ValidationError
from irb_watch.services.members import Member, MemberService
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_scan import make_source
from tests.test_admin_users_api import FakeVerifier

STUDY = make_source(5, name="Study 1", requires_login=True, ai_enabled=False)
PUBLIC = make_source(4, name="Rules")


class FakeSources:
    def __init__(self):
        self.items = {s.id: s for s in (STUDY, PUBLIC)}

    def get(self, source_id):
        if source_id not in self.items:
            raise NotFoundError("source not found")
        return self.items[source_id]


class FakeMemberStore:
    def __init__(self, users: FakeStore):
        self.rows: dict[tuple[int, str], str] = {}  # (source, user) -> added_by
        self._users = users

    def list(self, source_id):
        return [
            Member(u, self._users.profiles[u].email, None, "2026-11-09T00:00:00+00:00")
            for (s, u) in self.rows
            if s == source_id
        ]

    def add(self, source_id, user_id, added_by):
        self.rows.setdefault((source_id, user_id), added_by)

    def remove(self, source_id, user_id):
        return self.rows.pop((source_id, user_id), None) is not None


@pytest.fixture
def env():
    users = FakeStore()
    users.add("admin-1", "a@example.com", role="admin")
    users.add("user-1", "u@example.com")
    users.add("gone", "g@example.com", deleted_at="2026-01-01T00:00:00+00:00")
    store, audit = FakeMemberStore(users), FakeAudit()
    service = MemberService(FakeSources(), users, store, audit)
    return service, store, audit, users


def test_admin_assigns_a_user_to_a_study(env):
    service, store, audit, _ = env
    member = service.add("admin-1", 5, " u@example.com ")
    assert (member.user_id, member.email) == ("user-1", "u@example.com")
    assert store.rows == {(5, "user-1"): "admin-1"}
    entry = audit.entries[-1]
    assert entry["event_type"] == "source.member_added" and entry["actor_id"] == "admin-1"
    assert entry["source_id"] == 5 and entry["payload"] == {"user_id": "user-1"}


def test_adding_twice_keeps_one_row(env):
    service, store, _, _ = env
    service.add("admin-1", 5, "u@example.com")
    service.add("admin-1", 5, "u@example.com")
    assert list(store.rows) == [(5, "user-1")]


def test_a_public_source_needs_no_assignment(env):
    service, store, audit, _ = env
    with pytest.raises(ValidationError, match="public"):
        service.add("admin-1", 4, "u@example.com")
    assert store.rows == {} and audit.entries == []


@pytest.mark.parametrize("email", ["nobody@example.com", "g@example.com"])
def test_unknown_or_deleted_users_cannot_be_assigned(env, email):
    service, store, audit, _ = env
    with pytest.raises(NotFoundError, match="no active user"):
        service.add("admin-1", 5, email)
    assert store.rows == {} and audit.entries == []


def test_email_is_matched_in_lower_case_too(env):
    service, _, _, _ = env
    assert service.add("admin-1", 5, "U@Example.com").user_id == "user-1"


def test_empty_email_is_refused(env):
    service, *_ = env
    with pytest.raises(ValidationError):
        service.add("admin-1", 5, "   ")


def test_unknown_source(env):
    service, *_ = env
    with pytest.raises(NotFoundError, match="source"):
        service.list(99)
    with pytest.raises(NotFoundError, match="source"):
        service.add("admin-1", 99, "u@example.com")


def test_no_audit_entry_means_no_change_in_who_can_read(env):
    _, store, _, users = env
    service = MemberService(FakeSources(), users, store, FakeAudit(fail_required=True))
    with pytest.raises(AuditUnavailableError):
        service.add("admin-1", 5, "u@example.com")
    assert store.rows == {}
    store.rows[(5, "user-1")] = "admin-1"
    with pytest.raises(AuditUnavailableError):
        service.remove("admin-1", 5, "user-1")
    assert (5, "user-1") in store.rows  # still a member: the removal was not allowed through


def test_remove_logs_and_deletes(env):
    service, store, audit, _ = env
    service.add("admin-1", 5, "u@example.com")
    service.remove("admin-1", 5, "user-1")
    assert store.rows == {}
    assert audit.entries[-1]["event_type"] == "source.member_removed"


def test_removing_someone_who_is_not_a_member(env):
    service, _, audit, _ = env
    with pytest.raises(NotFoundError, match="not assigned"):
        service.remove("admin-1", 5, "user-1")
    assert audit.entries == []


# ---- over HTTP -----------------------------------------------------------------------------


@pytest.fixture
def client(env):
    service, _, _, users = env
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: users
    app.dependency_overrides[get_member_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def auth(user):
    return {"Authorization": f"Bearer tok-{user}"}


def test_members_endpoints_for_an_admin(client):
    put = client.put("/admin/sources/5/members", json={"email": "u@example.com"}, headers=auth("admin-1"))
    assert put.status_code == 200 and put.json()["user_id"] == "user-1"
    listed = client.get("/admin/sources/5/members", headers=auth("admin-1"))
    assert [m["email"] for m in listed.json()] == ["u@example.com"]
    gone = client.delete("/admin/sources/5/members/user-1", headers=auth("admin-1"))
    assert gone.status_code == 204
    assert client.get("/admin/sources/5/members", headers=auth("admin-1")).json() == []


def test_members_endpoints_refuse_everyone_else(client, env):
    store = env[1]
    for method, path, body in (
        ("get", "/admin/sources/5/members", None),
        ("put", "/admin/sources/5/members", {"email": "u@example.com"}),
        ("delete", "/admin/sources/5/members/user-1", None),
    ):
        assert getattr(client, method)(path, **({"json": body} if body else {})).status_code == 401
        res = getattr(client, method)(path, headers=auth("user-1"), **({"json": body} if body else {}))
        assert res.status_code == 403, (method, path)
    assert store.rows == {}


def test_http_status_codes(client):
    h = auth("admin-1")
    assert client.put("/admin/sources/4/members", json={"email": "u@example.com"}, headers=h).status_code == 422
    assert client.put("/admin/sources/5/members", json={"email": "x@example.com"}, headers=h).status_code == 404
    assert client.get("/admin/sources/99/members", headers=h).status_code == 404
    assert client.delete("/admin/sources/5/members/user-1", headers=h).status_code == 404
