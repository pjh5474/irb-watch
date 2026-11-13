"""IRB pipeline phase B1: sharing a project (members, PM, organization admins, platform admins'
read-only support access, hand-over) and what it opens: pipelines, steps, files, shared step
drafts, their jobs, the work page and the reminders (offline)."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import (
    get_docgen_service,
    get_draft_job_service,
    get_pipeline_service,
    get_project_access_service,
    get_user_store,
    get_verifier,
)
from irb_watch.main import app
from irb_watch.services.docgen import DocgenService
from irb_watch.services.draft_jobs import DraftJobService
from irb_watch.services.errors import (
    AuditUnavailableError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from irb_watch.services.pipeline_admin import PipelineSetupService
from irb_watch.services.pipelines import PipelineService
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_docgen import NOW, FakeDocgenStore, FakeWriter
from tests.fakes_jobs import FakeJobStore, LaterRunner, inline_jobs
from tests.fakes_pipeline import FakePipelineSetupStore
from tests.fakes_pipelines import FakePipelineFiles, FakePipelineStore
from tests.fakes_projects import FakeProjectAccessStore
from tests.test_admin_users_api import FakeVerifier
from tests.test_pipelines import docx_bytes

HOSPITAL = 4
OWNER, CRA, PM, ADMIN, SUPPORT, STRANGER, OTHER_ORG = "owner", "cra", "pm", "orgadmin", "support", "stranger", "far"


class Env:
    def __init__(self) -> None:
        self.users = FakeStore()
        for uid in (OWNER, CRA, PM, ADMIN, STRANGER, OTHER_ORG):
            self.users.add(uid, f"{uid}@example.com")
        self.users.add(SUPPORT, "support@example.com", role="admin")  # a platform admin
        self.pstore = FakePipelineStore()
        self.pstore.add_project(1, OWNER, {HOSPITAL})
        self.store = FakeProjectAccessStore(self.users, self.pstore)
        self.store.org_role[ADMIN] = "admin"
        self.store.org_of[OTHER_ORG] = "org-2"
        self.store.org_of[SUPPORT] = "org-2"  # support works for another organization
        self.audit = FakeAudit()
        from irb_watch.services.project_access import ProjectAccessService

        self.access = ProjectAccessService(self.users, self.store, self.audit)
        setup = PipelineSetupService(FakePipelineSetupStore(), FakeAudit())
        self.pipelines = PipelineService(
            self.users, self.pstore, setup, FakePipelineFiles(), max_file_bytes=200_000,
            clock=lambda: NOW, access=self.access,
        )
        self.dstore = FakeDocgenStore(FakeAudit())
        self.docgen = DocgenService(
            self.users, self.dstore, FakeWriter(), FakeAudit(), daily_limit=5, clock=lambda: NOW,
            steps=self.pipelines,
        )
        self.pipeline = self.pipelines.start(OWNER, 1, HOSPITAL)
        self.step = min(self.pstore.steps([self.pipeline.id]), key=lambda s: s.position)

    def share(self, *uids):
        for uid in uids:
            self.access.add_member(OWNER, 1, uid)


@pytest.fixture
def env():
    return Env()


# -- who may open a project --------------------------------------------------------------------


def test_levels(env):
    env.share(CRA)
    levels = {u: env.access.level(u, 1) for u in (OWNER, CRA, ADMIN, SUPPORT, STRANGER, OTHER_ORG)}
    assert levels == {OWNER: "owner", CRA: "edit", ADMIN: "manage", SUPPORT: "read", STRANGER: None, OTHER_ORG: None}
    with pytest.raises(NotFoundError):
        env.access.require(STRANGER, 1)
    with pytest.raises(ForbiddenError, match="only read"):
        env.access.require(SUPPORT, 1, "edit")


def test_a_member_moved_to_another_organization_loses_access_at_once(env):
    env.share(CRA)
    env.store.org_of[CRA] = "org-2"
    assert env.access.level(CRA, 1) is None
    with pytest.raises(NotFoundError):
        env.pipelines.detail(CRA, env.pipeline.id)
    sharing = env.access.sharing(OWNER, 1)
    assert [(m.person.id, m.in_organization) for m in sharing.members] == [(CRA, False)]


def test_members_work_in_the_pipeline_and_support_only_reads(env):
    env.share(CRA)
    env.pipelines.update_step(CRA, env.pipeline.id, env.step.id, {"note": "by the CRA"})
    doc, _ = env.pipelines.upload(CRA, env.pipeline.id, env.step.id, "Protocol", "p.docx", docx_bytes("text"))
    env.pipelines.remove_step(CRA, env.pipeline.id, env.step.id)  # owner's choice: members may remove
    env.pipelines.restore_step(CRA, env.pipeline.id, env.step.id)
    assert [s.pipeline.id for s in env.pipelines.list_for_project(CRA, 1)] == [env.pipeline.id]
    # The platform admin from another organization reads everything and changes nothing.
    detail = env.pipelines.detail(SUPPORT, env.pipeline.id)
    restored = next(v.step for v in detail.steps if v.step.id == env.step.id)  # restored steps go last
    assert detail.access == "read" and restored.note == "by the CRA"
    assert env.pipelines.download_url(SUPPORT, env.pipeline.id, env.step.id, doc.id, 1)
    for change in (
        lambda: env.pipelines.update_step(SUPPORT, env.pipeline.id, env.step.id, {"note": "x"}),
        lambda: env.pipelines.add_step(SUPPORT, env.pipeline.id, "x", None),
        lambda: env.pipelines.remove(SUPPORT, env.pipeline.id),
        lambda: env.pipelines.start(SUPPORT, 1, HOSPITAL),
        lambda: env.pipelines.draft_inputs(SUPPORT, env.pipeline.id, env.step.id),
        lambda: env.pipelines.check_own_step(SUPPORT, env.step.id),
    ):
        with pytest.raises(ForbiddenError):
            change()
    with pytest.raises(NotFoundError):
        env.pipelines.detail(STRANGER, env.pipeline.id)


# -- members, PM, hand-over --------------------------------------------------------------------


def test_members_come_from_the_organization_and_only_once(env):
    with pytest.raises(ValidationError, match="organization"):
        env.access.add_member(OWNER, 1, OTHER_ORG)
    with pytest.raises(ConflictError, match="owns"):
        env.access.add_member(OWNER, 1, OWNER)
    env.share(CRA)
    with pytest.raises(ConflictError, match="already"):
        env.access.add_member(OWNER, 1, CRA)
    with pytest.raises(ForbiddenError):
        env.access.add_member(CRA, 1, PM)  # a member cannot add members
    assert [p.id for p in env.access.candidates(OWNER, 1)] == sorted([ADMIN, PM, STRANGER])
    assert env.audit.entries[-1]["event_type"] == "project.member_added"


def test_the_pm_manages_members_but_never_the_pm(env):
    env.access.set_pm(OWNER, 1, PM)
    env.access.add_member(PM, 1, CRA)
    env.access.remove_member(PM, 1, CRA)
    with pytest.raises(ForbiddenError):
        env.access.set_pm(PM, 1, None)
    with pytest.raises(ForbiddenError, match="change the PM"):
        env.access.remove_member(PM, 1, PM)
    # An organization admin moves the PM role; the old PM stays a member.
    env.share(CRA)
    env.access.set_pm(ADMIN, 1, CRA)
    roles = {m.person.id: m.role for m in env.access.sharing(OWNER, 1).members}
    assert roles == {CRA: "pm", PM: "member"}
    assert env.access.pms([1]) == {1: CRA}


def test_a_member_may_leave(env):
    env.share(CRA)
    env.access.leave(CRA, 1)
    assert env.access.level(CRA, 1) is None
    with pytest.raises(NotFoundError):
        env.access.leave(CRA, 1)


def test_hand_over_refusals_and_success(env):
    env.pstore.add_project(2, CRA, set())
    env.store.names[2] = "Project 1"  # the same name as project 1
    with pytest.raises(ConflictError, match="already has a project named"):
        env.access.hand_over(OWNER, 1, CRA, True)
    env.store.names[2] = "Other"
    for pid in range(3, 52):
        env.pstore.add_project(pid, CRA, set())
    with pytest.raises(ConflictError, match="50 projects"):
        env.access.hand_over(OWNER, 1, CRA, True)
    for pid in range(3, 52):
        del env.pstore.projects[pid]
    env.share(CRA)
    with pytest.raises(ForbiddenError):
        env.access.hand_over(CRA, 1, PM, True)  # a member is not the owner or an org admin
    env.access.hand_over(ADMIN, 1, CRA, keep_as_member=True)
    assert env.access.level(CRA, 1) == "owner" and env.access.level(OWNER, 1) == "edit"
    assert [m.person.id for m in env.access.sharing(CRA, 1).members] == [OWNER]
    assert env.audit.entries[-1]["event_type"] == "project.handed_over"


def test_hand_over_fails_closed_without_its_audit_entry(env):
    env.audit.fail_required = True
    with pytest.raises(AuditUnavailableError):
        env.access.hand_over(OWNER, 1, CRA, False)
    assert env.access.level(OWNER, 1) == "owner"


def test_shared_with_me_and_organization_projects(env):
    env.access.set_pm(OWNER, 1, PM)
    assert [(s.project.id, s.role, s.owner.id) for s in env.access.shared_with(PM)] == [(1, "pm", OWNER)]
    assert env.access.shared_with(CRA) == []
    listed = env.access.organization_projects(ADMIN, None)
    assert [(o.project.id, o.pm.id, o.members) for o in listed] == [(1, PM, 1)]
    assert [o.project.id for o in env.access.organization_projects(SUPPORT, "org-1")] == [1]
    with pytest.raises(ForbiddenError):
        env.access.organization_projects(CRA, None)


# -- shared drafts and their jobs --------------------------------------------------------------


def make_draft(env, user):
    doc, _ = env.pipelines.upload(OWNER, env.pipeline.id, env.step.id, "Protocol", "p.docx", docx_bytes("text"))
    return env.docgen.create_draft(user, [], None, [], "", [], step_id=env.step.id, step_files=[(doc.id, None)]).draft


def test_step_drafts_belong_to_the_project(env):
    env.share(CRA)
    draft = make_draft(env, OWNER)
    assert env.docgen.get_draft(CRA, draft.id).draft.id == draft.id
    env.docgen.save_edit(CRA, draft.id, "# edited by the CRA", 1, None)
    assert env.docgen.can_edit_draft(SUPPORT, draft.id) is False
    assert env.docgen.get_draft(SUPPORT, draft.id)  # read-only support access
    with pytest.raises(ForbiddenError):
        env.docgen.save_edit(SUPPORT, draft.id, "# no", 2, None)
    with pytest.raises(NotFoundError):
        env.docgen.get_draft(STRANGER, draft.id)
    with pytest.raises(ForbiddenError, match="author"):
        env.docgen.delete_draft(CRA, draft.id)  # members edit; deleting is the author's or an admin's
    env.docgen.delete_draft(ADMIN, draft.id)


def test_a_removed_or_moved_author_loses_their_step_drafts_too(env):
    env.share(CRA)
    draft = make_draft(env, CRA)
    env.access.remove_member(OWNER, 1, CRA)
    with pytest.raises(NotFoundError):
        env.docgen.get_draft(CRA, draft.id)
    # Only when the step itself is gone does the author keep the draft.
    del env.pstore.step_rows[env.step.id]
    assert env.docgen.get_draft(CRA, draft.id).draft.id == draft.id


def test_a_refused_pm_change_leaves_the_pm_as_it_was(env, monkeypatch):
    env.access.set_pm(OWNER, 1, PM)
    monkeypatch.setattr("irb_watch.services.project_access.MAX_MEMBERS", 1)
    with pytest.raises(ConflictError, match="at most 1"):
        env.access.set_pm(OWNER, 1, CRA)  # not a member yet, and the project is full
    assert env.access.pms([1]) == {1: PM}


def test_an_org_admin_who_is_the_pm_keeps_reminders_and_the_shared_list(env):
    env.access.set_pm(OWNER, 1, ADMIN)
    assert env.access.level(ADMIN, 1) == "manage"
    assert env.access.pms([1]) == {1: ADMIN}
    assert [s.project.id for s in env.access.shared_with(ADMIN)] == [1]


def test_a_hand_over_refused_by_the_database_changes_nothing(env, monkeypatch):
    env.share(CRA)

    def taken(*args):
        raise LookupError("name taken")  # the unique index, as in a race with a rename

    monkeypatch.setattr(env.store, "hand_over", taken)
    with pytest.raises(ConflictError, match="rename one first"):
        env.access.hand_over(OWNER, 1, CRA, True)
    assert env.access.level(OWNER, 1) == "owner" and env.access.level(CRA, 1) == "edit"


def test_two_members_cannot_start_two_drafts_of_one_step(env):
    env.share(CRA)
    runner = LaterRunner()
    jobs = DraftJobService(FakeJobStore(), runner, clock=lambda: datetime(2026, 11, 12, 1, tzinfo=UTC))
    jobs.start(OWNER, "draft", lambda: (lambda: None), step_id=env.step.id)
    with pytest.raises(ConflictError, match="being written"):
        jobs.start(CRA, "draft", lambda: (lambda: None), step_id=env.step.id)
    assert [j.user_id for j in jobs.for_steps([env.step.id], [])] == [OWNER]


# -- work page and reminders -------------------------------------------------------------------


def test_work_and_reminders_follow_the_project(env):
    env.access.set_pm(OWNER, 1, PM)
    env.share(CRA)
    env.pipelines.update_step(OWNER, env.pipeline.id, env.step.id, {"due_on": "2026-11-20"})
    for uid in (OWNER, PM, CRA, ADMIN):
        assert env.step.id in {w.step_id for w in env.pipelines.work_steps(uid)}, uid
    assert env.pipelines.work_steps(SUPPORT) == []  # read-only support access is not work
    assert {w.project_owner_id for w in env.pipelines.work_steps(CRA)} == {OWNER}
    found = env.pipelines.reminder_steps("2026-12-01")
    assert sorted(found) == [OWNER, PM]  # owner and PM, not every member (O27)
    env.access.hand_over(OWNER, 1, CRA, keep_as_member=False)
    assert sorted(env.pipelines.reminder_steps("2026-12-01")) == [CRA, PM]


# -- API ---------------------------------------------------------------------------------------


def header(uid):
    return {"Authorization": f"Bearer tok-{uid}"}


@pytest.fixture
def client(env):
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: env.users
    app.dependency_overrides[get_pipeline_service] = lambda: env.pipelines
    app.dependency_overrides[get_docgen_service] = lambda: env.docgen
    jobs = inline_jobs()
    app.dependency_overrides[get_draft_job_service] = lambda: jobs
    app.dependency_overrides[get_project_access_service] = lambda: env.access
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_sharing_api(env, client):
    assert client.post("/projects/1/members", headers=header(OWNER), json={"user_id": CRA}).status_code == 200
    r = client.put("/projects/1/pm", headers=header(ADMIN), json={"user_id": PM})
    body = r.json()
    assert r.status_code == 200 and {m["user"]["id"]: m["role"] for m in body["members"]} == {CRA: "member", PM: "pm"}
    mine = client.get("/projects/1/sharing", headers=header(CRA)).json()
    assert (mine["access"], mine["can_manage_members"], mine["is_member"]) == ("edit", False, True)
    assert client.get("/projects/1/sharing", headers=header(STRANGER)).status_code == 404
    shared = client.get("/projects/shared", headers=header(CRA)).json()
    assert shared[0]["owner"]["name"] == "owner@example.com"
    r = client.post("/projects/1/members", headers=header(SUPPORT), json={"user_id": STRANGER})
    assert r.status_code == 403 and "owner" in r.json()["detail"]
    # The step page tells read-only users and names who did what.
    client.patch(f"/pipelines/{env.pipeline.id}/steps/{env.step.id}", headers=header(CRA), json={"note": "x"})
    page = client.get(f"/pipelines/{env.pipeline.id}/steps/{env.step.id}", headers=header(SUPPORT)).json()
    assert (page["access"], page["shared"]) == ("read", True)
    history = client.get(f"/pipelines/{env.pipeline.id}/history", headers=header(OWNER)).json()
    assert history[0]["actor_name"] == "cra@example.com" and history[0]["mine"] is False
    r = client.patch(f"/pipelines/{env.pipeline.id}/steps/{env.step.id}", headers=header(SUPPORT), json={"note": "x"})
    assert r.status_code == 403 and "only read" in r.json()["detail"]
    assert client.post("/projects/1/leave", headers=header(CRA)).status_code == 204
    orgs = client.get("/organization/projects", headers=header(ADMIN)).json()
    assert orgs[0]["pm"]["id"] == PM
