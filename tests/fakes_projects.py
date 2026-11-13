"""In-memory stand-in for services.project_access.ProjectAccessStore. Its access_level mirrors
the database's private.project_access_level (migration project_sharing)."""

from datetime import UTC, datetime

from irb_watch.services.project_access import MemberRow, Person, ProjectAccessService, ProjectRow
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_pipelines import FakePipelineStore

ORG = "org-1"


class FakeProjectAccessStore:
    def __init__(self, users: FakeStore, pipelines: FakePipelineStore) -> None:
        self.users = users
        self.pipelines = pipelines  # its `projects` dict: project id -> owner
        self.names: dict[int, str] = {}  # project id -> name (default "Project <id>")
        self.project_orgs: dict[int, str] = {}  # default ORG
        self.org_of: dict[str, str] = {}  # user -> organization (default ORG)
        self.org_role: dict[str, str] = {}  # user -> owner | admin | member (default member)
        self.member_rows: dict[int, list[MemberRow]] = {}

    def _row(self, pid: int) -> ProjectRow | None:
        owner = self.pipelines.projects.get(pid)
        if owner is None:
            return None
        return ProjectRow(pid, owner, self.project_orgs.get(pid, ORG), self.names.get(pid, f"Project {pid}"))

    def _org(self, uid: str) -> str | None:
        return self.org_of.get(uid, ORG) if uid in self.users.profiles else None

    def access_level(self, project_id, user_id):
        p = self._row(project_id)
        if p is None or not user_id:
            return None
        if p.user_id == user_id:
            return "owner"
        in_org = self._org(user_id) == p.organization_id
        if in_org and self.org_role.get(user_id, "member") in ("owner", "admin"):
            return "manage"
        row = next((m for m in self.member_rows.get(project_id, []) if m.user_id == user_id), None)
        if row is not None and in_org:
            return "pm" if row.role == "pm" else "edit"
        profile = self.users.profiles.get(user_id)
        if profile is not None and profile.role == "admin" and profile.deleted_at is None:
            return "read"
        return None

    def workable_project_ids(self, user_id):
        return [pid for pid in self.pipelines.projects if self.access_level(pid, user_id) in ("owner", "manage", "pm", "edit")]

    def project(self, project_id):
        return self._row(project_id)

    def projects(self, project_ids):
        return [r for r in (self._row(p) for p in project_ids) if r is not None]

    def organization_projects(self, organization_id):
        return [r for r in self.projects(list(self.pipelines.projects)) if r.organization_id == organization_id]

    def owned_projects(self, user_id):
        return [r for r in self.projects(list(self.pipelines.projects)) if r.user_id == user_id]

    def members(self, project_ids):
        return {p: list(self.member_rows[p]) for p in project_ids if self.member_rows.get(p)}

    def memberships(self, user_id):
        return [(p, m.role) for p, rows in self.member_rows.items() for m in rows if m.user_id == user_id]

    def add_member(self, project_id, user_id, role, added_by):
        rows = self.member_rows.setdefault(project_id, [])
        if any(m.user_id == user_id for m in rows) or (role == "pm" and any(m.role == "pm" for m in rows)):
            raise LookupError("taken")
        rows.append(MemberRow(user_id, role, datetime.now(UTC).isoformat()))

    def set_pm(self, project_id, user_id, added_by):
        rows = [MemberRow(m.user_id, "member", m.added_at) if m.role == "pm" else m for m in self.member_rows.get(project_id, [])]
        if user_id is not None:
            if any(m.user_id == user_id for m in rows):
                rows = [MemberRow(m.user_id, "pm", m.added_at) if m.user_id == user_id else m for m in rows]
            else:
                rows.append(MemberRow(user_id, "pm", datetime.now(UTC).isoformat()))
        self.member_rows[project_id] = rows

    def hand_over(self, project_id, user_id, keep_old_owner, actor):
        old = self.pipelines.projects[project_id]
        name = self.names.get(project_id, f"Project {project_id}").strip().lower()
        if any(self.pipelines.projects.get(p) == user_id and self.names.get(p, f"Project {p}").strip().lower() == name
               for p in self.pipelines.projects if p != project_id):
            raise LookupError("name taken")
        self.remove_member(project_id, user_id)
        self.pipelines.projects[project_id] = user_id
        if keep_old_owner and not any(m.user_id == old for m in self.member_rows.get(project_id, [])):
            self.member_rows.setdefault(project_id, []).append(MemberRow(old, "member", datetime.now(UTC).isoformat()))

    def remove_member(self, project_id, user_id):
        self.member_rows[project_id] = [m for m in self.member_rows.get(project_id, []) if m.user_id != user_id]

    def people(self, user_ids):
        out = {}
        for uid in user_ids:
            p = self.users.profiles.get(uid)
            if p is not None:
                out[uid] = Person(uid, p.display_name or p.email or "", self._org(uid), p.deleted_at is None)
        return out

    def organization_people(self, organization_id):
        return [p for p in self.people(list(self.users.profiles)).values() if p.organization_id == organization_id]

    def organization_role(self, user_id):
        org = self._org(user_id)
        return (org, self.org_role.get(user_id, "member")) if org else (None, None)


def access_service(users: FakeStore, pipelines: FakePipelineStore) -> tuple[ProjectAccessService, FakeProjectAccessStore, FakeAudit]:
    store = FakeProjectAccessStore(users, pipelines)
    audit = FakeAudit()
    return ProjectAccessService(users, store, audit), store, audit
