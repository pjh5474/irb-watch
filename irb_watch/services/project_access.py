"""Sharing a project (IRB pipeline redesign, phase B1). Who may open a project, and its members.

Owner decisions (2026-11-13; docs/decisions.md "IRB pipeline redesign, phase B1: sharing"):
  * Levels (one rule, the database's private.project_access_level, read through the store):
      owner   the project's owner                        everything, incl. deleting the project
      manage  an owner/admin of the project's org        read, edit, members, PM, hand-over
      pm      the project's PM (still in its org)        read, edit, add/remove members
      edit    a member (still in its org)                read and edit, incl. soft removes
      read    a platform admin outside that              read everything, change nothing (support;
              organization                               no audit, owner's choice)
    Anyone else: "not found". A level too low for a change: 403 with the reason.
  * Members come from the project's organization; a user moved elsewhere loses access at once.
  * The PM is set by the owner or an organization admin; the PM adds and removes members but
    never changes the PM or the owner. A member may leave by themselves.
  * Hand-over (owner or org admin): to an active user of the same organization; refused when that
    user already has a project with the same name or owns 50 projects. Audit-logged first.
  * At most 30 members per project. No mail when someone is added (the app shows it).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from irb_watch.services.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from irb_watch.services.users import AuditSink, UserStore, active_profile

MAX_MEMBERS = 30
MAX_PROJECTS = 50  # per owner (same as the database trigger on insert)

READ = frozenset({"owner", "manage", "pm", "edit", "read"})
EDIT = frozenset({"owner", "manage", "pm", "edit"})
MEMBERS = frozenset({"owner", "manage", "pm"})
MANAGE = frozenset({"owner", "manage"})
NEEDS = {"read": READ, "edit": EDIT, "members": MEMBERS, "manage": MANAGE, "owner": frozenset({"owner"})}
REFUSALS = {
    "edit": "you can only read this project",
    "members": "only the project's owner, its PM or an organization admin can change its members",
    "manage": "only the project's owner or an organization admin can do this",
    "owner": "only the project's owner can do this",
}


@dataclass(frozen=True)
class ProjectRow:
    id: int
    user_id: str  # the owner
    organization_id: str
    name: str


@dataclass(frozen=True)
class MemberRow:
    user_id: str
    role: str  # pm | member
    added_at: str


@dataclass(frozen=True)
class Person:
    id: str
    name: str  # display name, else email
    organization_id: str | None
    active: bool


class ProjectAccessStore(Protocol):
    def access_level(self, project_id: int, user_id: str) -> str | None: ...
    def workable_project_ids(self, user_id: str) -> list[int]: ...
    def project(self, project_id: int) -> ProjectRow | None: ...
    def projects(self, project_ids: list[int]) -> list[ProjectRow]: ...
    def organization_projects(self, organization_id: str) -> list[ProjectRow]: ...
    def owned_projects(self, user_id: str) -> list[ProjectRow]: ...
    def members(self, project_ids: list[int]) -> dict[int, list[MemberRow]]: ...
    def memberships(self, user_id: str) -> list[tuple[int, str]]:
        """(project id, role) of the user's own member rows."""
        ...
    def add_member(self, project_id: int, user_id: str, role: str, added_by: str) -> None:
        """Raises LookupError when a unique index refuses (a member twice, a second PM)."""
        ...
    def set_pm(self, project_id: int, user_id: str | None, added_by: str) -> None:
        """In one transaction: the current PM becomes a member; user_id (if any) becomes the PM,
        added as a member first when needed. LookupError when a unique index refuses."""
        ...
    def remove_member(self, project_id: int, user_id: str) -> None: ...
    def hand_over(self, project_id: int, user_id: str, keep_old_owner: bool, actor: str) -> None:
        """In one transaction: the new owner's member row goes, the owner changes, the old owner
        stays as a member if asked. LookupError when the new owner has a project of that name."""
        ...
    def people(self, user_ids: list[str]) -> dict[str, Person]: ...
    def organization_people(self, organization_id: str) -> list[Person]: ...
    def organization_role(self, user_id: str) -> tuple[str | None, str | None]:
        """(organization id, organization role) of the user."""
        ...


@dataclass(frozen=True)
class MemberView:
    person: Person
    role: str  # pm | member
    in_organization: bool  # False: moved away; the row stays but grants nothing


@dataclass(frozen=True)
class Sharing:
    project: ProjectRow
    level: str
    owner: Person | None
    members: list[MemberView]

    @property
    def shared(self) -> bool:
        return bool(self.members)


@dataclass(frozen=True)
class SharedProject:
    project: ProjectRow
    owner: Person | None
    role: str  # the user's own: pm | member


@dataclass(frozen=True)
class OrgProject:
    project: ProjectRow
    owner: Person | None
    pm: Person | None
    members: int


class ProjectAccessService:
    def __init__(
        self,
        users: UserStore,
        store: ProjectAccessStore,
        audit: AuditSink,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._users = users
        self._store = store
        self._audit = audit
        self._clock = clock

    # -- the rule ------------------------------------------------------------------------------

    def level(self, user_id: str, project_id: int) -> str | None:
        return self._store.access_level(project_id, user_id)

    def require(self, user_id: str, project_id: int, need: str = "read") -> str:
        """The user's level on the project, or NotFoundError (no access at all) / ForbiddenError
        (a level too low for this; the reason says why)."""
        active_profile(self._users, user_id, "your account is not active")
        level = self.level(user_id, project_id)
        if level is None:
            raise NotFoundError("project not found")
        if level not in NEEDS[need]:
            raise ForbiddenError(REFUSALS[need])
        return level

    def workable_project_ids(self, user_id: str) -> list[int]:
        """Projects the user may work on (owner, manage, pm, edit): the work page, reminders."""
        return self._store.workable_project_ids(user_id)

    def pms(self, project_ids: list[int]) -> dict[int, str]:
        """project -> its PM, only while the PM may still work on it (still in the organization;
        an organization admin who is also the PM has the higher level 'manage')."""
        out = {}
        for pid, rows in self._store.members(project_ids).items():
            pm = next((m.user_id for m in rows if m.role == "pm"), None)
            if pm is not None and self.level(pm, pid) in ("pm", "manage"):
                out[pid] = pm
        return out

    def shared(self, project_id: int) -> bool:
        """The project has members (the draft dialog warns before a members-only board goes into
        a draft they would all read; O22)."""
        return bool(self._store.members([project_id]).get(project_id))

    def names(self, user_ids: list[str]) -> dict[str, str]:
        return {uid: p.name for uid, p in self._store.people(sorted(set(user_ids))).items()}

    # -- reading -------------------------------------------------------------------------------

    def _project(self, project_id: int) -> ProjectRow:
        project = self._store.project(project_id)
        if project is None:
            raise NotFoundError("project not found")
        return project

    def sharing(self, user_id: str, project_id: int) -> Sharing:
        level = self.require(user_id, project_id, "read")
        project = self._project(project_id)
        rows = self._store.members([project_id]).get(project_id, [])
        people = self._store.people([project.user_id, *(m.user_id for m in rows)])
        members = [
            MemberView(
                people.get(m.user_id) or Person(m.user_id, "", None, False),
                m.role,
                (people.get(m.user_id) and people[m.user_id].organization_id) == project.organization_id,
            )
            for m in sorted(rows, key=lambda m: (m.role != "pm", m.added_at))
        ]
        return Sharing(project, level, people.get(project.user_id), members)

    def candidates(self, user_id: str, project_id: int) -> list[Person]:
        """Active users of the project's organization who are not in it yet (to add or to hand
        the project over to)."""
        self.require(user_id, project_id, "members")
        project = self._project(project_id)
        taken = {project.user_id, *(m.user_id for m in self._store.members([project_id]).get(project_id, []))}
        return sorted(
            (p for p in self._store.organization_people(project.organization_id) if p.active and p.id not in taken),
            key=lambda p: p.name.lower(),
        )

    def shared_with(self, user_id: str) -> list[SharedProject]:
        """Projects shared with the user (as PM or member) that they may still open."""
        active_profile(self._users, user_id, "your account is not active")
        rows = dict(self._store.memberships(user_id))
        # A member who is also an organization admin has the higher level 'manage'.
        projects = [p for p in self._store.projects(list(rows)) if self.level(user_id, p.id) in ("pm", "edit", "manage")]
        owners = self._store.people([p.user_id for p in projects])
        return [SharedProject(p, owners.get(p.user_id), rows[p.id]) for p in sorted(projects, key=lambda p: p.name.lower())]

    def organization_projects(self, user_id: str, organization_id: str | None) -> list[OrgProject]:
        """Organization admins: their organization's projects. Platform admins: any organization
        (read-only outside their own)."""
        profile = active_profile(self._users, user_id, "your account is not active")
        own_org, org_role = self._store.organization_role(user_id)
        if organization_id is None:
            organization_id = own_org
        is_org_admin = organization_id == own_org and org_role in ("owner", "admin")
        if not is_org_admin and profile.role != "admin":
            raise ForbiddenError("only an organization's admins can list its projects")
        if organization_id is None:
            return []
        projects = self._store.organization_projects(organization_id)
        members = self._store.members([p.id for p in projects])
        people = self._store.people(
            [p.user_id for p in projects] + [m.user_id for rows in members.values() for m in rows]
        )
        out = []
        for p in sorted(projects, key=lambda p: p.name.lower()):
            rows = members.get(p.id, [])
            pm = next((m.user_id for m in rows if m.role == "pm"), None)
            out.append(OrgProject(p, people.get(p.user_id), people.get(pm) if pm else None, len(rows)))
        return out

    # -- changing ------------------------------------------------------------------------------

    def _org_user(self, project: ProjectRow, user_id: str) -> Person:
        person = self._store.people([user_id]).get(user_id)
        if person is None or not person.active:
            raise NotFoundError("user not found")
        if person.organization_id != project.organization_id:
            raise ValidationError("only users of the project's organization can be added")
        return person

    def _event(self, actor: str, kind: str, project: ProjectRow, payload: dict, *, required: bool = False) -> None:
        record = self._audit.record_required if required else self._audit.record
        record("user", f"project.{kind}", actor, title=f"Project {kind.replace('_', ' ')}",
               payload={"project_id": project.id, **payload})

    def add_member(self, actor: str, project_id: int, user_id: str) -> None:
        self.require(actor, project_id, "members")
        project = self._project(project_id)
        if user_id == project.user_id:
            raise ConflictError("this user owns the project")
        self._org_user(project, user_id)
        rows = self._store.members([project_id]).get(project_id, [])
        if any(m.user_id == user_id for m in rows):
            raise ConflictError("this user is already in the project")
        if len(rows) >= MAX_MEMBERS:
            raise ConflictError(f"a project can have at most {MAX_MEMBERS} members")
        try:
            self._store.add_member(project_id, user_id, "member", actor)
        except LookupError as exc:
            raise ConflictError("this user was just added by someone else; reload the page") from exc
        self._event(actor, "member_added", project, {"user_id": user_id})

    def remove_member(self, actor: str, project_id: int, user_id: str) -> None:
        level = self.require(actor, project_id, "members")
        project = self._project(project_id)
        row = next((m for m in self._store.members([project_id]).get(project_id, []) if m.user_id == user_id), None)
        if row is None:
            raise NotFoundError("this user is not in the project")
        if row.role == "pm" and level == "pm":
            raise ForbiddenError("only the project's owner or an organization admin can change the PM")
        self._store.remove_member(project_id, user_id)
        self._event(actor, "member_removed", project, {"user_id": user_id, "role": row.role})

    def leave(self, actor: str, project_id: int) -> None:
        active_profile(self._users, actor, "your account is not active")
        project = self._project(project_id)
        row = next((m for m in self._store.members([project_id]).get(project_id, []) if m.user_id == actor), None)
        if row is None:
            raise NotFoundError("you are not a member of this project")
        self._store.remove_member(project_id, actor)
        self._event(actor, "member_left", project, {"role": row.role})

    def set_pm(self, actor: str, project_id: int, user_id: str | None) -> None:
        """user_id None: no PM (the PM stays a member)."""
        self.require(actor, project_id, "manage")
        project = self._project(project_id)
        rows = self._store.members([project_id]).get(project_id, [])
        current = next((m.user_id for m in rows if m.role == "pm"), None)
        if user_id == current:
            return
        is_member = user_id is not None and any(m.user_id == user_id for m in rows)
        # Every check before any write, so a refused change leaves the PM as it was.
        if user_id is not None:
            if user_id == project.user_id:
                raise ConflictError("the owner cannot also be the PM")
            self._org_user(project, user_id)
            if not is_member and len(rows) >= MAX_MEMBERS:
                raise ConflictError(f"a project can have at most {MAX_MEMBERS} members")
        try:
            self._store.set_pm(project_id, user_id, actor)
        except LookupError as exc:
            raise ConflictError("the PM was just changed by someone else; reload the page") from exc
        self._event(actor, "pm_set", project, {"from": current, "to": user_id})

    def hand_over(self, actor: str, project_id: int, user_id: str, keep_as_member: bool) -> None:
        self.require(actor, project_id, "manage")
        project = self._project(project_id)
        if user_id == project.user_id:
            raise ConflictError("this user already owns the project")
        self._org_user(project, user_id)
        owned = self._store.owned_projects(user_id)
        if any(p.name.strip().lower() == project.name.strip().lower() for p in owned):
            raise ConflictError(f'the new owner already has a project named "{project.name}"; rename one first')
        if len(owned) >= MAX_PROJECTS:
            raise ConflictError(f"the new owner already owns {MAX_PROJECTS} projects (the limit)")
        rows = self._store.members([project_id]).get(project_id, [])
        if keep_as_member and len(rows) >= MAX_MEMBERS and not any(m.user_id == user_id for m in rows):
            raise ConflictError(f"a project can have at most {MAX_MEMBERS} members")
        # Fail closed: no audit entry, no hand-over.
        self._event(
            actor, "handed_over", project, {"from": project.user_id, "to": user_id, "keep": keep_as_member},
            required=True,
        )
        # One transaction in the database (public.hand_over_project): the new owner's member row
        # goes, the owner changes, the old owner may stay as a member; all or nothing.
        try:
            self._store.hand_over(project_id, user_id, keep_as_member, actor)
        except LookupError as exc:
            raise ConflictError(
                f'the new owner already has a project named "{project.name}"; rename one first'
            ) from exc
