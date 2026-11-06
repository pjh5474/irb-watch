"""Supabase-backed implementations of the protocols in services.users."""

import logging

from supabase import Client
from supabase_auth.errors import AuthApiError

from irb_watch.services.errors import AuditUnavailableError
from irb_watch.services.users import EmailTakenError, Profile

log = logging.getLogger(__name__)

# Roughly 100 years: Supabase has no "forever" value for bans.
BAN_FOREVER = "876000h"
EMAIL_TAKEN_CODES = {"email_exists", "user_already_exists"}


def _profile(row: dict) -> Profile:
    return Profile(
        id=row["id"],
        email=row.get("email"),
        display_name=row.get("display_name"),
        role=row["role"],
        created_at=row["created_at"],
        deleted_at=row.get("deleted_at"),
    )


class SupabaseUserStore:
    def __init__(self, client: Client) -> None:
        self._c = client

    def _one(self, column: str, value: str) -> Profile | None:
        rows = self._c.table("profiles").select("*").eq(column, value).limit(1).execute().data
        return _profile(rows[0]) if rows else None

    def get_profile(self, user_id: str) -> Profile | None:
        return self._one("id", user_id)

    def get_profile_by_email(self, email: str) -> Profile | None:
        return self._one("email", email)

    def list_profiles(
        self, limit: int, offset: int, include_deleted: bool
    ) -> tuple[list[Profile], int]:
        query = self._c.table("profiles").select("*", count="exact")
        if not include_deleted:
            query = query.is_("deleted_at", "null")
        result = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
        return [_profile(r) for r in result.data], result.count or 0

    def count_active_admins(self) -> int:
        result = (
            self._c.table("profiles")
            .select("id", count="exact")
            .eq("role", "admin")
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        return result.count or 0

    def _update(self, user_id: str, values: dict) -> Profile:
        rows = self._c.table("profiles").update(values).eq("id", user_id).execute().data
        if not rows:
            raise LookupError(f"profile {user_id} not found")
        return _profile(rows[0])

    def set_role(self, user_id: str, role: str) -> Profile:
        return self._update(user_id, {"role": role})

    def set_deleted_at(self, user_id: str, deleted_at: str | None) -> Profile:
        return self._update(user_id, {"deleted_at": deleted_at})


class SupabaseAuthAdmin:
    def __init__(self, client: Client) -> None:
        self._admin = client.auth.admin

    def create_user(self, email: str, password: str, display_name: str | None) -> str:
        attributes: dict = {"email": email, "password": password, "email_confirm": True}
        if display_name:
            attributes["user_metadata"] = {"display_name": display_name}
        try:
            response = self._admin.create_user(attributes)
        except AuthApiError as exc:
            if getattr(exc, "code", None) in EMAIL_TAKEN_CODES:
                raise EmailTakenError from exc
            raise
        return str(response.user.id)

    def set_banned(self, user_id: str, banned: bool) -> None:
        self._admin.update_user_by_id(user_id, {"ban_duration": BAN_FOREVER if banned else "none"})

    def hard_delete(self, user_id: str) -> None:
        self._admin.delete_user(user_id)


class SupabaseAuditSink:
    """record(): best effort, a failed log write must not undo an action that already happened.
    record_required(): fail closed, for actions that must not happen without their log entry."""

    def __init__(self, client: Client) -> None:
        self._c = client

    def _insert(
        self,
        category: str,
        event_type: str,
        actor_id: str | None,
        title: str | None,
        payload: dict | None,
        source_id: int | None,
        document_id: int | None,
        summary: str | None,
        priority: str | None,
    ) -> None:
        self._c.table("audit_logs").insert(
            {
                "category": category,
                "event_type": event_type,
                "actor_id": actor_id,
                "source_id": source_id,
                "document_id": document_id,
                "title": title,
                "summary": summary,
                "priority": priority,
                "payload": payload or {},
            }
        ).execute()

    def record(
        self,
        category: str,
        event_type: str,
        actor_id: str | None,
        title: str | None = None,
        payload: dict | None = None,
        *,
        source_id: int | None = None,
        document_id: int | None = None,
        summary: str | None = None,
        priority: str | None = None,
    ) -> None:
        try:
            self._insert(
                category, event_type, actor_id, title, payload, source_id, document_id,
                summary, priority,
            )
        except Exception:
            log.exception("could not write audit log entry %s", event_type)

    def record_required(
        self,
        category: str,
        event_type: str,
        actor_id: str | None,
        title: str | None = None,
        payload: dict | None = None,
        *,
        source_id: int | None = None,
        document_id: int | None = None,
        summary: str | None = None,
        priority: str | None = None,
    ) -> None:
        try:
            self._insert(
                category, event_type, actor_id, title, payload, source_id, document_id,
                summary, priority,
            )
        except Exception as exc:
            log.exception("could not write required audit log entry %s", event_type)
            raise AuditUnavailableError(
                "could not record this action in the audit log, so it was not carried out; "
                "try again shortly"
            ) from exc
