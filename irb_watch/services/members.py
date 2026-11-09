"""Admin-assigned readers of members-only sources (studies).

A members-only source is readable by admins and by the users listed here (see the database
rule private.can_read_source). Only admins change the list. Every change is written to the
audit log first and fails closed: no audit entry, no change in who can read confidential data.
"""

from dataclasses import dataclass
from typing import Protocol

from irb_watch.services.errors import NotFoundError, ValidationError
from irb_watch.services.sources import Source
from irb_watch.services.users import AuditSink, UserStore


@dataclass(frozen=True)
class Member:
    user_id: str
    email: str | None
    display_name: str | None
    added_at: str | None


class MemberStore(Protocol):
    def list(self, source_id: int) -> list[Member]: ...
    def add(self, source_id: int, user_id: str, added_by: str | None) -> None:
        """Idempotent: adding an existing member changes nothing."""
        ...

    def remove(self, source_id: int, user_id: str) -> bool:
        """True if the user was a member."""
        ...


class SourceLookup(Protocol):
    def get(self, source_id: int) -> Source: ...


class MemberService:
    def __init__(
        self, sources: SourceLookup, users: UserStore, store: MemberStore, audit: AuditSink
    ) -> None:
        self._sources = sources
        self._users = users
        self._store = store
        self._audit = audit

    def list(self, source_id: int) -> list[Member]:
        self._sources.get(source_id)  # NotFoundError for an unknown source
        return self._store.list(source_id)

    def add(self, actor_id: str, source_id: int, email: str) -> Member:
        source = self._sources.get(source_id)
        if not source.requires_login:
            raise ValidationError("this source is public; nobody needs to be assigned to it")
        email = email.strip()
        if not email:
            raise ValidationError("email must not be empty")
        profile = self._users.get_profile_by_email(email) or self._users.get_profile_by_email(
            email.lower()
        )
        if profile is None or profile.deleted_at is not None:
            raise NotFoundError("no active user with that email")
        self._audit.record_required(
            "admin",
            "source.member_added",
            actor_id,
            title=f"Reader added to {source.name}",
            payload={"user_id": profile.id},
            source_id=source_id,
        )
        self._store.add(source_id, profile.id, actor_id)
        return next(m for m in self._store.list(source_id) if m.user_id == profile.id)

    def remove(self, actor_id: str, source_id: int, user_id: str) -> None:
        source = self._sources.get(source_id)
        if not any(m.user_id == user_id for m in self._store.list(source_id)):
            raise NotFoundError("that user is not assigned to this source")
        self._audit.record_required(
            "admin",
            "source.member_removed",
            actor_id,
            title=f"Reader removed from {source.name}",
            payload={"user_id": user_id},
            source_id=source_id,
        )
        self._store.remove(source_id, user_id)
