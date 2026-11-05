import pytest

from irb_watch.services.users import (
    ConflictError,
    NotFoundError,
    UpstreamError,
    UserService,
    ValidationError,
)
from tests.fakes import FakeAudit, FakeAuth, FakeStore


@pytest.fixture
def env():
    store = FakeStore()
    auth = FakeAuth(store)
    audit = FakeAudit()
    store.add("admin-1", "boss@example.com", role="admin")
    service = UserService(store, auth, audit, min_password_length=8)
    return service, store, auth, audit


# ---- create ------------------------------------------------------------------


def test_create_user_lowercases_email_and_logs(env):
    service, store, auth, audit = env
    profile = service.create_user("admin-1", "  New@Example.COM ", "longenough", "New")
    assert profile.email == "new@example.com"
    assert profile.role == "user"
    assert auth.created == [("new@example.com", "longenough", "New")]
    assert audit.entries[-1]["event_type"] == "user.created"
    assert audit.entries[-1]["actor_id"] == "admin-1"


def test_create_admin_sets_role(env):
    service, *_ = env
    assert service.create_user("admin-1", "a2@example.com", "longenough", role="admin").role == "admin"


def test_short_password_rejected_and_nothing_created(env):
    service, store, auth, audit = env
    with pytest.raises(ValidationError):
        service.create_user("admin-1", "x@example.com", "short")
    assert auth.created == [] and audit.entries == []


def test_duplicate_email_conflicts(env):
    service, *_ = env
    with pytest.raises(ConflictError, match="already exists"):
        service.create_user("admin-1", "BOSS@example.com", "longenough")


def test_duplicate_email_of_deleted_user_points_to_restore(env):
    service, store, *_ = env
    store.add("gone", "gone@example.com", deleted_at="2026-01-01T00:00:00+00:00")
    with pytest.raises(ConflictError, match="restore"):
        service.create_user("admin-1", "gone@example.com", "longenough")


def test_email_race_reported_as_conflict(env):
    service, store, auth, _ = env
    auth.email_race = True
    with pytest.raises(ConflictError):
        service.create_user("admin-1", "race@example.com", "longenough")


def test_missing_profile_after_signup_removes_the_account(env):
    service, store, auth, audit = env
    auth.trigger_works = False
    with pytest.raises(UpstreamError):
        service.create_user("admin-1", "t@example.com", "longenough")
    assert auth.hard_deleted == ["new-1"]
    assert audit.entries == []


# ---- role --------------------------------------------------------------------


def test_promote_user(env):
    service, store, *_ = env
    store.add("u1", "u1@example.com")
    assert service.change_role("admin-1", "u1", "admin").role == "admin"


def test_same_role_is_a_no_op(env):
    service, store, *_ = env
    store.add("u1", "u1@example.com")
    assert service.change_role("admin-1", "u1", "user").role == "user"


def test_cannot_demote_yourself(env):
    service, store, *_ = env
    store.add("admin-2", "b@example.com", role="admin")
    with pytest.raises(ConflictError, match="yourself"):
        service.change_role("admin-1", "admin-1", "user")


def test_cannot_demote_last_admin(env):
    service, store, *_ = env
    store.add("u1", "u1@example.com")
    with pytest.raises(ConflictError, match="last remaining admin"):
        service.change_role("u1", "admin-1", "user")


def test_can_demote_one_of_two_admins(env):
    service, store, *_ = env
    store.add("admin-2", "b@example.com", role="admin")
    assert service.change_role("admin-1", "admin-2", "user").role == "user"


def test_deleted_admin_does_not_count_as_active(env):
    service, store, *_ = env
    store.add("admin-2", "b@example.com", role="admin", deleted_at="2026-01-01T00:00:00+00:00")
    store.add("u1", "u1@example.com")
    with pytest.raises(ConflictError, match="last remaining admin"):
        service.change_role("u1", "admin-1", "user")


def test_role_change_on_deleted_user_conflicts(env):
    service, store, *_ = env
    store.add("gone", "g@example.com", deleted_at="2026-01-01T00:00:00+00:00")
    with pytest.raises(ConflictError):
        service.change_role("admin-1", "gone", "admin")


def test_role_change_unknown_user(env):
    service, *_ = env
    with pytest.raises(NotFoundError):
        service.change_role("admin-1", "nope", "admin")


# ---- delete ------------------------------------------------------------------


def test_delete_sets_deleted_at_bans_and_logs(env):
    service, store, auth, audit = env
    store.add("u1", "u1@example.com")
    deleted = service.delete_user("admin-1", "u1")
    assert deleted.deleted_at is not None
    assert "u1" in auth.banned
    assert audit.entries[-1]["event_type"] == "user.deleted"


def test_cannot_delete_yourself(env):
    service, store, *_ = env
    store.add("admin-2", "b@example.com", role="admin")
    with pytest.raises(ConflictError, match="yourself"):
        service.delete_user("admin-1", "admin-1")


def test_cannot_delete_last_admin(env):
    service, store, *_ = env
    store.add("u1", "u1@example.com")
    with pytest.raises(ConflictError, match="last remaining admin"):
        service.delete_user("u1", "admin-1")


def test_delete_twice_conflicts(env):
    service, store, *_ = env
    store.add("u1", "u1@example.com")
    service.delete_user("admin-1", "u1")
    with pytest.raises(ConflictError, match="already deleted"):
        service.delete_user("admin-1", "u1")


def test_ban_failure_leaves_user_untouched(env):
    service, store, auth, audit = env
    store.add("u1", "u1@example.com")
    auth.fail_ban = True
    with pytest.raises(UpstreamError):
        service.delete_user("admin-1", "u1")
    assert store.get_profile("u1").deleted_at is None
    assert audit.entries == []


def test_delete_unknown_user(env):
    service, *_ = env
    with pytest.raises(NotFoundError):
        service.delete_user("admin-1", "nope")


# ---- restore -----------------------------------------------------------------


def test_restore_clears_deleted_at_and_unbans(env):
    service, store, auth, audit = env
    store.add("u1", "u1@example.com")
    service.delete_user("admin-1", "u1")
    restored = service.restore_user("admin-1", "u1")
    assert restored.deleted_at is None
    assert "u1" not in auth.banned
    assert audit.entries[-1]["event_type"] == "user.restored"


def test_restore_of_active_user_conflicts(env):
    service, store, *_ = env
    store.add("u1", "u1@example.com")
    with pytest.raises(ConflictError, match="not deleted"):
        service.restore_user("admin-1", "u1")


def test_unban_failure_changes_nothing(env):
    service, store, auth, _ = env
    store.add("u1", "u1@example.com")
    service.delete_user("admin-1", "u1")
    auth.fail_unban = True
    with pytest.raises(UpstreamError):
        service.restore_user("admin-1", "u1")
    assert store.get_profile("u1").deleted_at is not None
    assert "u1" in auth.banned


def test_profile_update_failure_rebans(env):
    service, store, auth, _ = env
    store.add("u1", "u1@example.com")
    service.delete_user("admin-1", "u1")
    store.fail_set_deleted_at_to_none = True
    with pytest.raises(UpstreamError):
        service.restore_user("admin-1", "u1")
    assert "u1" in auth.banned
    assert store.get_profile("u1").deleted_at is not None
