"""End-to-end check against the REAL Supabase project from .env.<APP_ENV>.

Skipped unless RUN_LIVE=1 (PowerShell: $env:RUN_LIVE=1; uv run pytest -m live).
Creates two throwaway users and removes them again. Audit-log rows cannot be removed
(the log is append-only), so each run leaves a few rows behind.
"""

import os
import uuid

import pytest

from irb_watch.config import get_settings
from irb_watch.db.client import get_supabase_client
from irb_watch.db.supabase_users import SupabaseAuditSink, SupabaseAuthAdmin, SupabaseUserStore
from irb_watch.services.users import ConflictError, UserService

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to run"),
]


def test_user_lifecycle_against_real_project():
    client = get_supabase_client()
    auth = SupabaseAuthAdmin(client)
    store = SupabaseUserStore(client)
    service = UserService(store, auth, SupabaseAuditSink(client), get_settings().min_password_length)

    tag = uuid.uuid4().hex[:10]
    created: list[str] = []
    try:
        # Signup trigger: creating an auth user must produce a profile with role 'user'.
        a = service.create_user(None, f"irbwatch-test-a-{tag}@example.com", "temporary-pass-1")
        created.append(a.id)
        assert a.role == "user" and a.deleted_at is None

        # Role change by an admin; the database trigger must write an audit entry.
        a = service.change_role(a.id, a.id, "admin")
        entries = (
            client.table("audit_logs")
            .select("event_type")
            .eq("event_type", "role.changed")
            .contains("payload", {"user_id": a.id})
            .execute()
            .data
        )
        assert entries, "role change trigger did not write an audit_logs row"

        b = service.create_user(a.id, f"irbwatch-test-b-{tag}@example.com", "temporary-pass-2")
        created.append(b.id)

        # Soft delete: profile marked and the auth account banned.
        deleted = service.delete_user(a.id, b.id)
        assert deleted.deleted_at is not None
        banned_until = getattr(client.auth.admin.get_user_by_id(b.id).user, "banned_until", None)
        assert banned_until is not None, "auth account was not banned"

        # Deleted user's email stays reserved.
        with pytest.raises(ConflictError, match="restore"):
            service.create_user(a.id, f"irbwatch-test-b-{tag}@example.com", "temporary-pass-3")

        restored = service.restore_user(a.id, b.id)
        assert restored.deleted_at is None
        assert getattr(client.auth.admin.get_user_by_id(b.id).user, "banned_until", None) is None

        # A is the only active admin among the test users but other admins may exist, so only
        # assert the guard when A really is the last one.
        if store.count_active_admins() == 1:
            with pytest.raises(ConflictError, match="last remaining admin"):
                service.change_role(b.id, a.id, "user")
    finally:
        for user_id in created:
            auth.hard_delete(user_id)
