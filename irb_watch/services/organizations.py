"""Organizations: admin management and monthly statistics.

Rules (owner, 2026-11-11; see docs/decisions.md "Organization admin page and statistics"):
  * An organization only groups users and is the unit for statistics. It never grants or
    limits access. Only platform admins (profiles.role = admin) manage organizations.
  * One organization per user. A new account joins Default (database trigger); the admin may
    pick another organization when creating the user, or move the user later.
  * Soft delete (deleted_at), refused for Default and while the organization still has users
    (deleted accounts included: they can be restored). Restore is refused if a live
    organization has the same name.
  * Statistics per calendar month in SCAN_TIMEZONE. Usage counts where it happened (each audit
    row is stamped with the user's organization when written).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from irb_watch.core import org_stats as s
from irb_watch.core.weekly import UserInfo, Window
from irb_watch.services.errors import (
    ConflictError,
    NotFoundError,
    UpstreamError,
    ValidationError,
)
from irb_watch.services.users import AuditSink

DEFAULT_ORGANIZATION_ID = "00000000-0000-4000-8000-000000000001"
MAX_NAME_CHARS = 200


@dataclass(frozen=True)
class Organization:
    id: str
    name: str
    created_at: str
    deleted_at: str | None
    member_count: int  # every user in it, deleted accounts included

    @property
    def is_default(self) -> bool:
        return self.id == DEFAULT_ORGANIZATION_ID


@dataclass(frozen=True)
class Member:
    user_id: str
    email: str | None
    display_name: str | None
    role: str  # the platform role (user | admin)
    created_at: str
    deleted_at: str | None
    org_role: str = "member"  # owner | admin | member in this organization (phase B1: owner and
    # admin open every pipeline project of the organization)


ORG_ROLES = ("owner", "admin", "member")


class NameTakenError(Exception):
    """Raised by the store when a live organization already has the name."""


class OrganizationStore(Protocol):
    def list_organizations(self) -> list[Organization]: ...
    def get_organization(self, organization_id: str) -> Organization | None: ...
    def create_organization(self, name: str) -> Organization: ...
    def rename_organization(self, organization_id: str, name: str) -> Organization: ...
    def set_deleted_at(self, organization_id: str, deleted_at: str | None) -> Organization: ...
    def members(self, organization_id: str) -> list[Member]: ...
    def organization_of(self, user_ids: list[str]) -> dict[str, str]: ...
    def all_memberships(self) -> dict[str, str]: ...
    def user_exists(self, user_id: str) -> bool: ...
    def set_user_organization(self, user_id: str, organization_id: str) -> None:
        """A move makes the user a plain member there (the role never travels)."""
        ...
    def set_organization_role(self, user_id: str, organization_id: str, role: str) -> bool:
        """False: the user is not in that organization."""
        ...
    def usage(self, start: datetime, end: datetime, types: list[str]) -> list[s.UsageRow]: ...
    def accounts(self) -> list[UserInfo]: ...


@dataclass(frozen=True)
class MonthStats:
    month: str
    organizations: list[s.OrgTotals]


@dataclass(frozen=True)
class OrgMonthStats:
    month: str
    organization: Organization
    totals: s.OrgTotals
    users: list[s.UserTotals]


def _clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise ValidationError("the organization needs a name")
    if len(name) > MAX_NAME_CHARS:
        raise ValidationError(f"the name may have at most {MAX_NAME_CHARS} characters")
    return name


class OrganizationService:
    def __init__(
        self,
        store: OrganizationStore,
        audit: AuditSink,
        *,
        timezone: str = "Asia/Seoul",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._audit = audit
        self._tz = timezone
        self._clock = clock

    # -- read ---------------------------------------------------------------------------------

    def list_organizations(self, include_deleted: bool = False) -> list[Organization]:
        orgs = self._store.list_organizations()
        if not include_deleted:
            orgs = [o for o in orgs if o.deleted_at is None]
        # Default first, then by name.
        return sorted(orgs, key=lambda o: (not o.is_default, o.name.lower(), o.id))

    def get(self, organization_id: str) -> Organization:
        org = self._store.get_organization(organization_id)
        if org is None:
            raise NotFoundError("organization not found")
        return org

    def require_live(self, organization_id: str) -> Organization:
        org = self.get(organization_id)
        if org.deleted_at is not None:
            raise ConflictError("this organization is deleted; restore it or pick another")
        return org

    def members(self, organization_id: str) -> list[Member]:
        self.get(organization_id)
        return self._store.members(organization_id)

    def organization_names(self, user_ids: list[str]) -> dict[str, Organization]:
        """user id -> their organization, for the user list."""
        if not user_ids:
            return {}
        membership = self._store.organization_of(user_ids)
        orgs = {o.id: o for o in self._store.list_organizations()}
        return {uid: orgs[oid] for uid, oid in membership.items() if oid in orgs}

    # -- change -------------------------------------------------------------------------------

    def create(self, actor_id: str, name: str) -> Organization:
        name = _clean_name(name)
        try:
            org = self._store.create_organization(name)
        except NameTakenError as exc:
            raise ConflictError("an organization with this name already exists") from exc
        self._audit.record(
            "admin", "organization.created", actor_id, title=f"Organization {name} created",
            payload={"organization_id": org.id, "name": name},
        )
        return org

    def rename(self, actor_id: str, organization_id: str, name: str) -> Organization:
        org = self.require_live(organization_id)
        name = _clean_name(name)
        if name == org.name:
            return org
        try:
            renamed = self._store.rename_organization(organization_id, name)
        except NameTakenError as exc:
            raise ConflictError("an organization with this name already exists") from exc
        self._audit.record(
            "admin", "organization.renamed", actor_id, title=f"Organization {org.name} renamed to {name}",
            payload={"organization_id": organization_id, "from": org.name, "to": name},
        )
        return renamed

    def delete(self, actor_id: str, organization_id: str) -> Organization:
        org = self.get(organization_id)
        if org.is_default:
            raise ConflictError("the Default organization cannot be deleted")
        if org.deleted_at is not None:
            raise ConflictError("this organization is already deleted")
        if org.member_count > 0:
            raise ConflictError(
                f"move its {org.member_count} user(s) to another organization first "
                "(deleted accounts too: they can be restored)"
            )
        deleted = self._store.set_deleted_at(organization_id, self._clock().isoformat())
        self._audit.record(
            "admin", "organization.deleted", actor_id, title=f"Organization {org.name} deleted",
            payload={"organization_id": organization_id, "name": org.name},
        )
        return deleted

    def restore(self, actor_id: str, organization_id: str) -> Organization:
        org = self.get(organization_id)
        if org.deleted_at is None:
            raise ConflictError("this organization is not deleted")
        try:
            restored = self._store.set_deleted_at(organization_id, None)
        except NameTakenError as exc:
            raise ConflictError(
                "a live organization has the same name; rename one of them first"
            ) from exc
        self._audit.record(
            "admin", "organization.restored", actor_id, title=f"Organization {org.name} restored",
            payload={"organization_id": organization_id, "name": org.name},
        )
        return restored

    def move_user(self, actor_id: str, user_id: str, organization_id: str) -> Organization:
        """Puts the user in the organization. The audit entry is written first (fail closed):
        the move decides where the user's usage counts from now on."""
        if not self._store.user_exists(user_id):
            raise NotFoundError("user not found")
        target = self.require_live(organization_id)
        current = self._store.organization_of([user_id]).get(user_id)
        if current == organization_id:
            return target
        self._audit.record_required(
            "admin", "user.organization_changed", actor_id,
            title=f"User moved to organization {target.name}",
            payload={"user_id": user_id, "from": current, "to": organization_id},
        )
        self._store.set_user_organization(user_id, organization_id)
        return target

    def set_organization_role(self, actor_id: str, user_id: str, role: str) -> str:
        """Platform admins set who is an organization's owner, admin or member. Owners and admins
        open every pipeline project of their organization (phase B1). Audit-logged first."""
        if role not in ORG_ROLES:
            raise ValidationError(f"the organization role must be one of {', '.join(ORG_ROLES)}")
        organization_id = self._store.organization_of([user_id]).get(user_id)
        if organization_id is None:
            raise NotFoundError("this user is in no organization")
        self._audit.record_required(
            "admin", "user.organization_role_changed", actor_id,
            title=f"Organization role set to {role}",
            payload={"user_id": user_id, "organization_id": organization_id, "role": role},
        )
        if not self._store.set_organization_role(user_id, organization_id, role):
            raise NotFoundError("this user is in no organization")
        return role

    def place_new_user(self, actor_id: str, user_id: str, organization_id: str) -> None:
        """After creating an account (it is in Default already): moves it if another
        organization was picked. A failure says where the user ended up."""
        if organization_id == DEFAULT_ORGANIZATION_ID:
            return
        try:
            self.move_user(actor_id, user_id, organization_id)
        except Exception as exc:
            raise UpstreamError(
                "the user was created in the Default organization but could not be moved; "
                "move them on the Organizations page"
            ) from exc

    # -- statistics ---------------------------------------------------------------------------

    def _window(self, month: str | None) -> tuple[str, Window]:
        """The month (default: the current one in the time zone) and its window."""
        now = self._clock()
        if month is None:
            month = now.astimezone(ZoneInfo(self._tz)).strftime("%Y-%m")
        try:
            return month, s.month_window(month, self._tz, now)
        except s.MonthError as exc:
            raise ValidationError(str(exc)) from exc

    @staticmethod
    def _ref(org: Organization) -> s.OrgRef:
        return s.OrgRef(org.id, org.name, org.deleted_at is not None)

    def month_stats(self, month: str | None = None) -> MonthStats:
        month, window = self._window(month)
        orgs = self.list_organizations(include_deleted=True)
        usage = self._store.usage(window.start, window.end, s.USAGE_EVENTS)
        rows = s.org_totals(
            window,
            [self._ref(o) for o in orgs],
            self._store.all_memberships(),
            self._store.accounts(),
            usage,
        )
        return MonthStats(month, rows)

    def organization_stats(self, organization_id: str, month: str | None = None) -> OrgMonthStats:
        org = self.get(organization_id)
        month, window = self._window(month)
        usage = self._store.usage(window.start, window.end, s.USAGE_EVENTS)
        accounts = self._store.accounts()
        members = {m.user_id: m.email for m in self._store.members(organization_id) if m.deleted_at is None}
        totals = s.org_totals(
            window,
            [s.OrgRef(org.id, org.name, False)],
            dict.fromkeys(members, org.id),
            accounts,
            usage,
        )[0]
        return OrgMonthStats(
            month, org, totals, s.user_totals(org.id, members, accounts, usage)
        )
