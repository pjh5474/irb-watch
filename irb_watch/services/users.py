"""Admin user management: create, list, change role, soft delete, restore.

Rules (see docs/decisions.md):
  * Accounts are created by admins with a temporary password; email is auto-confirmed.
  * Delete is soft: profiles.deleted_at is set AND the auth account is banned.
  * An admin cannot demote or delete themselves, nor the last active admin.
  * Role changes are logged by a database trigger; create/delete/restore are logged here.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

Role = Literal["user", "admin"]


@dataclass(frozen=True)
class Profile:
    id: str
    email: str | None
    display_name: str | None
    role: str
    created_at: str
    deleted_at: str | None


class UserServiceError(Exception):
    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(UserServiceError):
    status_code = 404


class ConflictError(UserServiceError):
    status_code = 409


class ValidationError(UserServiceError):
    status_code = 422


class UpstreamError(UserServiceError):
    status_code = 502


class EmailTakenError(Exception):
    """Raised by AuthAdmin.create_user when the email already has an auth account."""


class UserStore(Protocol):
    def get_profile(self, user_id: str) -> Profile | None: ...
    def get_profile_by_email(self, email: str) -> Profile | None: ...
    def list_profiles(
        self, limit: int, offset: int, include_deleted: bool
    ) -> tuple[list[Profile], int]: ...
    def count_active_admins(self) -> int: ...
    def set_role(self, user_id: str, role: str) -> Profile: ...
    def set_deleted_at(self, user_id: str, deleted_at: str | None) -> Profile: ...


class AuthAdmin(Protocol):
    def create_user(self, email: str, password: str, display_name: str | None) -> str: ...
    def set_banned(self, user_id: str, banned: bool) -> None: ...
    def hard_delete(self, user_id: str) -> None: ...


class AuditSink(Protocol):
    def record(
        self,
        category: str,
        event_type: str,
        actor_id: str | None,
        title: str | None = None,
        payload: dict | None = None,
    ) -> None: ...


class UserService:
    def __init__(
        self,
        store: UserStore,
        auth: AuthAdmin,
        audit: AuditSink,
        min_password_length: int = 8,
    ) -> None:
        self._store = store
        self._auth = auth
        self._audit = audit
        self._min_password_length = min_password_length

    # -- create -----------------------------------------------------------------

    def create_user(
        self,
        actor_id: str | None,
        email: str,
        password: str,
        display_name: str | None = None,
        role: Role = "user",
    ) -> Profile:
        email = email.strip().lower()
        if len(password) < self._min_password_length:
            raise ValidationError(
                f"password must be at least {self._min_password_length} characters"
            )

        existing = self._store.get_profile_by_email(email)
        if existing is not None:
            raise ConflictError(self._email_taken_message(existing))

        try:
            user_id = self._auth.create_user(email, password, display_name)
        except EmailTakenError as exc:
            raise ConflictError("a user with this email already exists") from exc

        profile = self._store.get_profile(user_id)
        if profile is None:
            # The signup trigger did not create a profile: do not leave a half-made account.
            self._auth.hard_delete(user_id)
            raise UpstreamError("account was created without a profile and has been removed")

        if role == "admin":
            profile = self._store.set_role(user_id, "admin")

        self._audit.record(
            "admin",
            "user.created",
            actor_id,
            title=f"User {email} created",
            payload={"user_id": user_id, "email": email, "role": role},
        )
        return profile

    @staticmethod
    def _email_taken_message(existing: Profile) -> str:
        if existing.deleted_at is not None:
            return "a deleted user has this email; restore that user instead"
        return "a user with this email already exists"

    # -- read -------------------------------------------------------------------

    def list_users(
        self, limit: int = 50, offset: int = 0, include_deleted: bool = False
    ) -> tuple[list[Profile], int]:
        return self._store.list_profiles(limit, offset, include_deleted)

    def _require_profile(self, user_id: str) -> Profile:
        profile = self._store.get_profile(user_id)
        if profile is None:
            raise NotFoundError("user not found")
        return profile

    # -- role -------------------------------------------------------------------

    def change_role(self, actor_id: str, target_id: str, role: Role) -> Profile:
        profile = self._require_profile(target_id)
        if profile.deleted_at is not None:
            raise ConflictError("user is deleted; restore them first")
        if profile.role == role:
            return profile
        if role != "admin":
            self._guard_not_removing_admin_access(actor_id, profile, "demote")
        # The audit entry comes from the database trigger on profiles.role.
        return self._store.set_role(target_id, role)

    # -- delete / restore -------------------------------------------------------

    def delete_user(self, actor_id: str, target_id: str) -> Profile:
        profile = self._require_profile(target_id)
        if profile.deleted_at is not None:
            raise ConflictError("user is already deleted")
        self._guard_not_removing_admin_access(actor_id, profile, "delete")

        deleted = self._store.set_deleted_at(target_id, datetime.now(UTC).isoformat())
        try:
            self._auth.set_banned(target_id, True)
        except Exception as exc:
            self._store.set_deleted_at(target_id, None)
            raise UpstreamError("could not block the user's login; nothing was changed") from exc

        self._audit.record(
            "admin",
            "user.deleted",
            actor_id,
            title=f"User {profile.email} deleted",
            payload={"user_id": target_id, "email": profile.email},
        )
        return deleted

    def restore_user(self, actor_id: str, target_id: str) -> Profile:
        profile = self._require_profile(target_id)
        if profile.deleted_at is None:
            raise ConflictError("user is not deleted")

        try:
            self._auth.set_banned(target_id, False)
        except Exception as exc:
            raise UpstreamError("could not unblock the user's login; nothing was changed") from exc
        try:
            restored = self._store.set_deleted_at(target_id, None)
        except Exception as exc:
            self._auth.set_banned(target_id, True)
            raise UpstreamError("could not restore the profile; login stays blocked") from exc

        self._audit.record(
            "admin",
            "user.restored",
            actor_id,
            title=f"User {profile.email} restored",
            payload={"user_id": target_id, "email": profile.email},
        )
        return restored

    # -- guards -----------------------------------------------------------------

    def _guard_not_removing_admin_access(
        self, actor_id: str, profile: Profile, verb: str
    ) -> None:
        if profile.id == actor_id:
            raise ConflictError(f"you cannot {verb} yourself")
        if profile.role == "admin" and self._store.count_active_admins() <= 1:
            raise ConflictError(f"cannot {verb} the last remaining admin")
