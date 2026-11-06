"""In-memory stand-ins for the Supabase-backed store, auth admin and audit sink."""

import itertools
from datetime import UTC, datetime

from irb_watch.services.users import EmailTakenError, Profile


class FakeStore:
    def __init__(self) -> None:
        self.profiles: dict[str, Profile] = {}
        self.fail_set_deleted_at_to_none = False

    def add(
        self,
        user_id: str,
        email: str,
        role: str = "user",
        deleted_at: str | None = None,
    ) -> Profile:
        p = Profile(user_id, email, None, role, datetime.now(UTC).isoformat(), deleted_at)
        self.profiles[user_id] = p
        return p

    def get_profile(self, user_id):
        return self.profiles.get(user_id)

    def get_profile_by_email(self, email):
        return next((p for p in self.profiles.values() if p.email == email), None)

    def list_profiles(self, limit, offset, include_deleted):
        rows = [p for p in self.profiles.values() if include_deleted or p.deleted_at is None]
        rows.sort(key=lambda p: p.created_at, reverse=True)
        return rows[offset : offset + limit], len(rows)

    def count_active_admins(self):
        return sum(1 for p in self.profiles.values() if p.role == "admin" and p.deleted_at is None)

    def _replace(self, user_id, **changes):
        p = self.profiles[user_id]
        new = Profile(**{**p.__dict__, **changes})
        self.profiles[user_id] = new
        return new

    def set_role(self, user_id, role):
        return self._replace(user_id, role=role)

    def set_deleted_at(self, user_id, deleted_at):
        if deleted_at is None and self.fail_set_deleted_at_to_none:
            raise RuntimeError("database down")
        return self._replace(user_id, deleted_at=deleted_at)


class FakeAuth:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self._ids = itertools.count(1)
        self.banned: set[str] = set()
        self.hard_deleted: list[str] = []
        self.created: list[tuple[str, str, str | None]] = []
        self.trigger_works = True
        self.email_race = False
        self.fail_ban = False
        self.fail_unban = False

    def create_user(self, email, password, display_name):
        if self.email_race:
            raise EmailTakenError
        self.created.append((email, password, display_name))
        user_id = f"new-{next(self._ids)}"
        if self.trigger_works:
            self._store.add(user_id, email)
        return user_id

    def set_banned(self, user_id, banned):
        if banned and self.fail_ban:
            raise RuntimeError("auth down")
        if not banned and self.fail_unban:
            raise RuntimeError("auth down")
        (self.banned.add if banned else self.banned.discard)(user_id)

    def hard_delete(self, user_id):
        self.hard_deleted.append(user_id)


class FakeAudit:
    def __init__(self, fail_required: bool = False) -> None:
        self.entries: list[dict] = []
        self.fail_required = fail_required  # make record_required() fail like a down database

    def record_required(self, *args, **kwargs):
        if self.fail_required:
            from irb_watch.services.errors import AuditUnavailableError

            raise AuditUnavailableError("could not record this action in the audit log")
        return self.record(*args, **kwargs)

    def record(
        self,
        category,
        event_type,
        actor_id,
        title=None,
        payload=None,
        *,
        source_id=None,
        document_id=None,
        summary=None,
        priority=None,
    ):
        self.entries.append(
            {
                "category": category,
                "event_type": event_type,
                "actor_id": actor_id,
                "title": title,
                "payload": payload or {},
                "source_id": source_id,
                "document_id": document_id,
                "summary": summary,
                "priority": priority,
            }
        )
