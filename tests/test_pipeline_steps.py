"""IRB pipeline phase B2b: follow-up steps, numbering, subject, the IRB result, the suggested
status, the hospital page and the admin's results list (offline)."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import (
    get_docgen_service,
    get_draft_job_service,
    get_pipeline_service,
    get_pipeline_setup_service,
    get_project_access_service,
    get_user_store,
    get_verifier,
)
from irb_watch.main import app
from irb_watch.services.errors import ConflictError, NotFoundError, ValidationError
from irb_watch.services.pipeline_admin import (
    PipelineSetupService,
    StepResult,
    StepStatus,
    StepTypeEdit,
)
from irb_watch.services.pipelines import PipelineService, Step, display_order, suggested_status
from irb_watch.services.project_access import ProjectAccessService
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_pipeline import FOLLOW_UP_TYPE, SEED_RESULTS, FakePipelineSetupStore
from tests.fakes_pipelines import FakePipelineFiles, FakePipelineStore, StepParentError
from tests.fakes_projects import FakeProjectAccessStore
from tests.fakes_sites import FakeSiteStore
from tests.test_admin_users_api import FakeVerifier
from tests.test_pipelines import NoDrafts

NOW = datetime(2026, 11, 20, 1, tzinfo=UTC)  # 10:00 in Seoul
TODAY = "2026-11-20"
SNUH, EWHA = 4, 5
INITIAL, AMENDMENT, CONTINUING, CLOSING = 1, 2, 3, 6


class Env:
    def __init__(self) -> None:
        self.users = FakeStore()
        for uid in ("owner", "stranger"):
            self.users.add(uid, f"{uid}@example.com")
        self.users.add("admin", "admin@example.com", role="admin")
        self.pstore = FakePipelineStore()
        self.pstore.add_project(1, "owner", {SNUH, EWHA})
        self.pstore.add_project(2, "stranger", {SNUH})
        access_store = FakeProjectAccessStore(self.users, self.pstore)
        access_store.org_of["admin"] = "org-2"
        self.access = ProjectAccessService(self.users, access_store, FakeAudit())
        self.sstore = FakeSiteStore(self.pstore)
        self.setup_store = FakePipelineSetupStore()
        # The migration adds the closed status 완료 and archives 승인 / 반려.
        self.setup_store.status_rows["done"] = StepStatus("done", "완료", True, 5)
        self.audit = FakeAudit()
        self.setup = PipelineSetupService(self.setup_store, self.audit)
        self.service = PipelineService(
            self.users, self.pstore, self.setup, FakePipelineFiles(), max_file_bytes=200_000,
            clock=lambda: NOW, access=self.access, sites=self.sstore,
        )

    def pipeline(self):
        pipeline = self.service.start("owner", 1, SNUH)
        steps = {s.step_type_id: s for s in self.pstore.steps([pipeline.id])}
        return pipeline, steps


@pytest.fixture
def env():
    return Env()


# -- starting and numbering --------------------------------------------------------------------


def test_start_numbers_numbered_types_and_never_starts_a_follow_up(env):
    env.setup_store.save_template(None, [INITIAL, AMENDMENT, FOLLOW_UP_TYPE, AMENDMENT, CONTINUING], "admin")
    pipeline = env.service.start("owner", 1, SNUH)
    steps = sorted(env.pstore.steps([pipeline.id]), key=lambda s: s.position)
    assert [s.label for s in steps] == ["초기 심의", "1차 변경 심의", "2차 변경 심의", "1차 지속 심의"]


def test_added_steps_take_the_next_number_and_a_subject(env):
    pipeline, _ = env.pipeline()  # global order: 초기, 1차 변경, 1차 지속, 종료
    step = env.service.add_step("owner", pipeline.id, None, AMENDMENT, subject="  PT 2.0,   CRF 1.1 ")
    assert (step.sequence_no, step.label, step.subject) == (2, "2차 변경 심의", "PT 2.0, CRF 1.1")
    closing = env.service.add_step("owner", pipeline.id, None, CLOSING)
    assert closing.sequence_no is None and closing.label == "종료 보고"
    with pytest.raises(ValidationError, match="at most 200"):
        env.service.add_step("owner", pipeline.id, None, AMENDMENT, subject="x" * 201)


def test_a_removed_step_does_not_count_and_nothing_is_renumbered(env):
    pipeline, steps = env.pipeline()
    env.service.remove_step("owner", pipeline.id, steps[AMENDMENT].id)
    again = env.service.add_step("owner", pipeline.id, None, AMENDMENT)
    assert again.sequence_no == 1  # the live ones are counted
    assert env.pstore.get_step(steps[AMENDMENT].id).sequence_no == 1


def test_the_next_number_follows_the_highest_live_one_not_the_count(env):
    pipeline, steps = env.pipeline()  # 1차 변경 exists
    second = env.service.add_step("owner", pipeline.id, None, AMENDMENT)
    assert second.sequence_no == 2
    env.service.remove_step("owner", pipeline.id, steps[AMENDMENT].id)  # 1차 removed, 2차 stays
    assert env.service.add_step("owner", pipeline.id, None, AMENDMENT).sequence_no == 3  # not a second 2차
    env.service.update_step("owner", pipeline.id, second.id, {"sequence_no": 7})
    assert env.service.add_step("owner", pipeline.id, None, AMENDMENT).sequence_no == 8


def test_reorder_keeps_follow_ups_with_their_main_step(env):
    pipeline, steps = env.pipeline()
    a, b = steps[INITIAL], steps[AMENDMENT]
    a1 = env.service.add_step("owner", pipeline.id, None, FOLLOW_UP_TYPE, parent_step_id=a.id)
    a2 = env.service.add_step("owner", pipeline.id, None, FOLLOW_UP_TYPE, parent_step_id=a.id)
    rest = [steps[CONTINUING].id, steps[CLOSING].id]
    # A follow-up sent before its main step, and B between A and A's follow-ups.
    answer = env.service.reorder_steps("owner", pipeline.id, [a2.id, b.id, a.id, a1.id, *rest])
    assert [s.id for s in answer] == [b.id, a.id, a2.id, a1.id, *rest]  # follow-ups keep their sent order
    shown = [v.step.id for v in env.service.detail("owner", pipeline.id).steps]
    assert shown == [s.id for s in answer]  # the answer is what the overview shows


def test_number_and_subject_can_be_edited_and_cleared(env):
    pipeline, steps = env.pipeline()
    sid = steps[AMENDMENT].id
    step = env.service.update_step("owner", pipeline.id, sid, {"sequence_no": 4, "subject": "연구비 변경"})
    assert (step.label, step.subject) == ("4차 변경 심의", "연구비 변경")
    step = env.service.update_step("owner", pipeline.id, sid, {"sequence_no": None})
    assert step.label == "변경 심의"
    for bad in (0, 1000, "3"):
        with pytest.raises(ValidationError, match="1 to 999"):
            env.service.update_step("owner", pipeline.id, sid, {"sequence_no": bad})
    events = [e for e in env.pstore.event_rows if e.event_type == "step.updated"]
    assert events[0].payload["sequence_no"] == {"from": 1, "to": 4}


# -- follow-ups --------------------------------------------------------------------------------


def test_follow_ups_hang_under_their_main_step_in_numbered_rounds(env):
    pipeline, steps = env.pipeline()
    main = steps[INITIAL]
    first = env.service.add_step("owner", pipeline.id, None, FOLLOW_UP_TYPE, parent_step_id=main.id)
    second = env.service.add_step("owner", pipeline.id, None, FOLLOW_UP_TYPE, parent_step_id=main.id)
    other = env.service.add_step("owner", pipeline.id, None, FOLLOW_UP_TYPE, parent_step_id=steps[AMENDMENT].id)
    assert (first.parent_step_id, first.label) == (main.id, "1차 보완/시정 답변")
    assert second.label == "2차 보완/시정 답변"
    assert other.label == "1차 보완/시정 답변"  # rounds are counted per main step
    # An answer to a follow-up answers the same main step (one level).
    third = env.service.add_step("owner", pipeline.id, "질의답변", None, parent_step_id=second.id)
    assert third.parent_step_id == main.id and third.sequence_no is None

    order = [v.step.id for v in env.service.detail("owner", pipeline.id).steps]
    assert order[:4] == [main.id, first.id, second.id, third.id]
    page = env.service.step_page("owner", pipeline.id, second.id)
    assert page.parent.id == main.id
    assert (page.previous_step_id, page.next_step_id) == (first.id, third.id)  # the overview's order
    assert [s.id for s in env.service.step_page("owner", pipeline.id, main.id).follow_ups] == [first.id, second.id, third.id]


def test_follow_up_rules(env):
    pipeline, steps = env.pipeline()
    with pytest.raises(ValidationError, match="needs the step it answers"):
        env.service.add_step("owner", pipeline.id, None, FOLLOW_UP_TYPE)
    with pytest.raises(ValidationError, match="takes a follow-up type or a free name"):
        env.service.add_step("owner", pipeline.id, None, AMENDMENT, parent_step_id=steps[INITIAL].id)
    other = env.service.start("stranger", 2, SNUH)
    foreign = next(iter(env.pstore.steps([other.id])))
    with pytest.raises(NotFoundError, match="not a live step of this pipeline"):
        env.service.add_step("owner", pipeline.id, "답변", None, parent_step_id=foreign.id)
    env.service.remove_step("owner", pipeline.id, steps[CLOSING].id)
    with pytest.raises(NotFoundError):
        env.service.add_step("owner", pipeline.id, "답변", None, parent_step_id=steps[CLOSING].id)


def test_the_database_refusal_of_a_parent_is_a_clear_conflict(env, monkeypatch):
    pipeline, steps = env.pipeline()

    def refuse(pipeline_id, values):
        raise StepParentError("a follow-up step answers a main step, not another follow-up")

    monkeypatch.setattr(env.pstore, "add_step", refuse)
    with pytest.raises(ConflictError, match="answers a main step.*reload the page"):
        env.service.add_step("owner", pipeline.id, "답변", None, parent_step_id=steps[INITIAL].id)


def test_a_step_with_follow_ups_cannot_be_removed_and_a_follow_up_waits_for_its_main_step(env):
    pipeline, steps = env.pipeline()
    main = steps[INITIAL]
    answer = env.service.add_step("owner", pipeline.id, None, FOLLOW_UP_TYPE, parent_step_id=main.id)
    with pytest.raises(ConflictError, match="1 follow-up step.*1차 보완/시정 답변"):
        env.service.remove_step("owner", pipeline.id, main.id)
    env.service.remove_step("owner", pipeline.id, answer.id)
    env.service.remove_step("owner", pipeline.id, main.id)
    with pytest.raises(ConflictError, match="the step it answers .초기 심의. is removed"):
        env.service.restore_step("owner", pipeline.id, answer.id)
    env.service.restore_step("owner", pipeline.id, main.id)
    assert env.service.restore_step("owner", pipeline.id, answer.id).deleted_at is None


def test_the_database_catches_a_removal_racing_a_new_follow_up(env, monkeypatch):
    pipeline, steps = env.pipeline()
    main = steps[INITIAL]
    env.service.add_step("owner", pipeline.id, None, FOLLOW_UP_TYPE, parent_step_id=main.id)
    real = env.pstore.steps
    # The service's own check reads before the follow-up was added (the race); the trigger
    # (here the fake's) refuses the removal.
    monkeypatch.setattr(env.pstore, "steps", lambda ids: [s for s in real(ids) if s.parent_step_id is None])
    with pytest.raises(ConflictError, match="has follow-up steps; remove them first; reload the page"):
        env.service.remove_step("owner", pipeline.id, main.id)
    assert env.pstore.get_step(main.id).deleted_at is None


def test_a_follow_up_racing_the_removal_of_its_main_step_is_refused(env, monkeypatch):
    pipeline, steps = env.pipeline()
    main = steps[INITIAL]
    real = env.pstore.steps
    snapshot = list(real([pipeline.id]))  # read before the main step was removed
    env.service.remove_step("owner", pipeline.id, main.id)
    monkeypatch.setattr(env.pstore, "steps", lambda ids: snapshot)
    with pytest.raises(ConflictError, match="just removed; reload the page"):
        env.service.add_step("owner", pipeline.id, "답변", None, parent_step_id=main.id)


def test_a_follow_up_whose_main_step_is_gone_is_a_main_step_on_both_pages(env):
    a = env.sstore.create_site(1, SNUH, "owner", "01", "김 PI", "")
    pipeline = env.service.start("owner", 1, site_id=a.id)
    steps = {s.step_type_id: s for s in env.pstore.steps([pipeline.id])}
    answer = env.service.add_step("owner", pipeline.id, None, FOLLOW_UP_TYPE, parent_step_id=steps[INITIAL].id)
    # The main step removed behind the service's back (a race the triggers did not see).
    env.pstore.step_rows[steps[INITIAL].id] = replace(steps[INITIAL], deleted_at="2026-11-19T00:00:00+00:00")
    overview = [v.step.id for v in env.service.detail("owner", pipeline.id).steps if v.step.deleted_at is None]
    assert answer.id in overview
    hospital = env.service.hospital_page("owner", 1, SNUH).sites[0]
    assert answer.id in [s.id for s in hospital.steps] and hospital.follow_ups == {}


def test_a_follow_up_whose_main_step_was_purged_shows_as_a_main_step():
    rows = [
        Step(1, 9, None, "main", 1, "not_started"),
        Step(2, 9, None, "answer", 5, "not_started", parent_step_id=1),
        Step(3, 9, None, "orphan", 2, "not_started", parent_step_id=77),
        Step(4, 9, None, "gone", 3, "not_started", deleted_at="2026-11-01"),
    ]
    assert [s.id for s in display_order(rows)] == [1, 2, 3, 4]


# -- the IRB result and the suggested status --------------------------------------------------


def test_result_fills_the_decision_date_and_leaves_the_status(env):
    pipeline, steps = env.pipeline()
    sid = steps[INITIAL].id
    step = env.service.update_step("owner", pipeline.id, sid, {"result_key": "approved"})
    assert (step.result_key, step.decided_on, step.status_key) == ("approved", TODAY, "not_started")
    page = env.service.step_page("owner", pipeline.id, sid)
    assert page.suggested_status == "done" and not page.needs_answer
    # A date sent with the result wins; clearing the result keeps the date.
    step = env.service.update_step("owner", pipeline.id, sid, {"result_key": "rejected", "decided_on": "2026-11-02"})
    assert step.decided_on == "2026-11-02"
    step = env.service.update_step("owner", pipeline.id, sid, {"result_key": None})
    assert step.result_key is None and step.decided_on == "2026-11-02"
    assert env.service.step_page("owner", pipeline.id, sid).suggested_status is None


def test_a_result_that_needs_an_answer_offers_the_follow_up(env):
    pipeline, steps = env.pipeline()
    sid = steps[INITIAL].id
    env.service.update_step("owner", pipeline.id, sid, {"result_key": "administrative_revision"})
    page = env.service.step_page("owner", pipeline.id, sid)
    assert page.needs_answer and page.suggested_status == "revisions_requested"
    assert [t.id for t in page.step_types] == [FOLLOW_UP_TYPE]
    env.service.update_step("owner", pipeline.id, sid, {"status_key": "revisions_requested"})
    assert env.service.step_page("owner", pipeline.id, sid).suggested_status is None  # already there


def test_an_archived_result_still_counts_for_both_hints(env):
    pipeline, steps = env.pipeline()
    sid = steps[INITIAL].id
    env.service.update_step("owner", pipeline.id, sid, {"result_key": "administrative_revision"})
    env.setup.archive_result("admin", "administrative_revision")
    page = env.service.step_page("owner", pipeline.id, sid)
    assert page.needs_answer and page.suggested_status == "revisions_requested"  # they agree


def test_unknown_or_archived_results_are_refused(env):
    pipeline, steps = env.pipeline()
    env.setup.archive_result("admin", "rejected")
    for key in ("rejected", "nope"):
        with pytest.raises(ValidationError, match="unknown or archived result"):
            env.service.update_step("owner", pipeline.id, steps[INITIAL].id, {"result_key": key})


def test_suggested_status_rules():
    statuses = [
        StepStatus("not_started", "시작전", False, 1),
        StepStatus("revisions_requested", "보완요청", False, 2),
        StepStatus("done", "완료", True, 3),
        StepStatus("not_needed", "해당없음", True, 4),
    ]
    step = Step(1, 9, None, "s", 1, "not_started")
    r = {k: StepResult(k, label, a, n, i) for i, (k, label, a, n) in enumerate(SEED_RESULTS, 1)}
    assert suggested_status(step, r["approved"], statuses) == "done"
    assert suggested_status(step, r["rejected"], statuses) == "done"  # a final answer too
    assert suggested_status(step, r["corrective_approval"], statuses) == "revisions_requested"  # needs an answer
    assert suggested_status(step, None, statuses) is None
    # 완료 archived: the first live closed status; no 보완요청: nothing offered.
    without = [s if s.key != "done" else StepStatus("done", "완료", True, 3, "x") for s in statuses]
    assert suggested_status(step, r["approved"], without) == "not_needed"
    assert suggested_status(step, r["revise_and_resubmit"], statuses[:1] + statuses[2:]) is None


# -- the hospital page -------------------------------------------------------------------------


def test_hospital_page_lines_up_the_sites_of_a_hospital(env):
    a = env.sstore.create_site(1, SNUH, "owner", "02", "이 PI", "")
    b = env.sstore.create_site(1, SNUH, "owner", "01", "김 PI", "")
    env.sstore.create_site(1, EWHA, "owner", "03", "박 PI", "")
    pa = env.service.start("owner", 1, site_id=a.id)
    env.sstore.create_site(1, SNUH, "owner", "", "최 PI", "")  # no pipeline yet
    initial = next(s for s in env.pstore.steps([pa.id]) if s.step_type_id == INITIAL)
    env.service.add_step("owner", pa.id, None, FOLLOW_UP_TYPE, parent_step_id=initial.id)
    page = env.service.hospital_page("owner", 1, SNUH)
    assert page.hospital_name == "SNUH" and page.access == "owner"
    assert [(h.site.pi_name, h.pipeline_id is not None) for h in page.sites] == [
        ("김 PI", False), ("이 PI", True), ("최 PI", False),  # by site number, unnumbered last
    ]
    site_a = page.sites[1]
    assert [s.label for s in site_a.steps] == ["초기 심의", "1차 변경 심의", "1차 지속 심의", "종료 보고"]
    assert site_a.follow_ups == {initial.id: 1}
    assert b.id == page.sites[0].site.id


def test_hospital_page_is_for_project_readers_and_project_hospitals(env):
    with pytest.raises(NotFoundError):
        env.service.hospital_page("stranger", 1, SNUH)
    with pytest.raises(NotFoundError, match="hospital not found"):
        env.service.hospital_page("owner", 1, 6)
    assert env.service.hospital_page("admin", 1, EWHA).access == "read"  # platform support: read only


# -- admin: results, categories, the default order -------------------------------------------


def test_admin_results_list(env):
    added = env.setup.add_result("admin", " 보류 ", False, False)
    assert added.label == "보류" and added.position == 7
    with pytest.raises(ConflictError, match="already exists"):
        env.setup.add_result("admin", "보류", True, False)
    changed = env.setup.update_result("admin", added.key, label="심의 연기", needs_answer=True)
    assert (changed.label, changed.needs_answer) == ("심의 연기", True)
    keys = [r.key for r in env.setup.results()]
    assert [r.key for r in env.setup.reorder_results("admin", keys[::-1])] == keys[::-1]
    with pytest.raises(ValidationError, match="exactly once"):
        env.setup.reorder_results("admin", keys[:2])
    env.setup.archive_result("admin", added.key)
    assert added.key not in [r.key for r in env.setup.results()]
    with pytest.raises(ConflictError, match="archived"):
        env.setup.update_result("admin", added.key, label="x")
    env.setup.restore_result("admin", added.key)
    events = [e["event_type"] for e in env.audit.entries]
    assert "pipeline.result_added" in events and "pipeline.result_restored" in events


def test_follow_up_types_stay_out_of_the_default_order(env):
    with pytest.raises(ValidationError, match="cannot be in the order: 보완/시정 답변"):
        env.setup.save_template("admin", None, [INITIAL, FOLLOW_UP_TYPE])
    with pytest.raises(ConflictError, match="remove it from the order first"):
        env.setup.update_step_type("admin", AMENDMENT, StepTypeEdit(name="변경 심의", category="follow_up"))
    with pytest.raises(ValidationError, match="the category must be one of"):
        env.setup.create_step_type("admin", StepTypeEdit(name="X", category="weird"))
    made = env.setup.create_step_type("admin", StepTypeEdit(name="재심의", category="follow_up", numbered=True))
    assert (made.category, made.numbered) == ("follow_up", True)


def test_a_type_in_use_cannot_switch_to_or_from_follow_up(env):
    safety = 4  # not in the default order
    env.setup_store.steps_of_type = {safety: 2, FOLLOW_UP_TYPE: 1}
    with pytest.raises(ConflictError, match="2 live step.s. use this type"):
        env.setup.update_step_type("admin", safety, StepTypeEdit(name="중대한 이상사례 보고", category="follow_up"))
    with pytest.raises(ConflictError, match="1 live step.s. use this type"):
        env.setup.update_step_type("admin", FOLLOW_UP_TYPE, StepTypeEdit(name="보완/시정 답변", category="report"))
    # Other changes of a type in use are fine, and so is a switch once nothing uses it.
    assert env.setup.update_step_type("admin", safety, StepTypeEdit(name="중대한 이상사례 보고", category="report")).category == "report"
    env.setup_store.steps_of_type = {}
    assert env.setup.update_step_type("admin", FOLLOW_UP_TYPE, StepTypeEdit(name="X", category="report")).category == "report"


# -- API ---------------------------------------------------------------------------------------


@pytest.fixture
def api(env):
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: env.users
    app.dependency_overrides[get_pipeline_service] = lambda: env.service
    app.dependency_overrides[get_pipeline_setup_service] = lambda: env.setup
    app.dependency_overrides[get_docgen_service] = NoDrafts
    app.dependency_overrides[get_draft_job_service] = NoDrafts
    app.dependency_overrides[get_project_access_service] = lambda: env.access
    yield TestClient(app)
    app.dependency_overrides.clear()


OWNER = {"Authorization": "Bearer tok-owner"}
ADMIN = {"Authorization": "Bearer tok-admin"}
STRANGER = {"Authorization": "Bearer tok-stranger"}


def test_api_follow_up_result_and_hospital_page(api, env):
    pid = api.post("/projects/1/pipelines", headers=OWNER, json={"hospital_id": SNUH}).json()["id"]
    detail = api.get(f"/pipelines/{pid}", headers=OWNER).json()
    main = detail["steps"][0]
    assert main["label"] == "초기 심의" and detail["steps"][1]["label"] == "1차 변경 심의"
    assert {r["key"] for r in detail["results"]} >= {"approved", "administrative_revision"}
    r = api.patch(f"/pipelines/{pid}/steps/{main['id']}", headers=OWNER, json={"result_key": "administrative_revision"})
    assert r.status_code == 200 and r.json()["decided_on"] == TODAY
    r = api.post(
        f"/pipelines/{pid}/steps", headers=OWNER,
        json={"step_type_id": FOLLOW_UP_TYPE, "parent_step_id": main["id"], "subject": "1차 행정수정"},
    )
    assert r.status_code == 201 and r.json()["label"] == "1차 보완/시정 답변"
    answer = r.json()
    page = api.get(f"/pipelines/{pid}/steps/{main['id']}", headers=OWNER).json()
    assert page["needs_answer"] and page["suggested_status"] == "revisions_requested"
    assert [f["id"] for f in page["follow_ups"]] == [answer["id"]]
    assert [t["id"] for t in page["follow_up_types"]] == [FOLLOW_UP_TYPE]
    child = api.get(f"/pipelines/{pid}/steps/{answer['id']}", headers=OWNER).json()
    assert child["parent"]["id"] == main["id"] and child["step"]["subject"] == "1차 행정수정"
    r = api.delete(f"/pipelines/{pid}/steps/{main['id']}", headers=OWNER)
    assert r.status_code == 409 and "follow-up" in r.json()["detail"]

    hospital = api.get(f"/projects/1/hospitals/{SNUH}", headers=OWNER).json()
    assert hospital["hospital_name"] == "SNUH" and len(hospital["sites"]) == 1
    assert hospital["sites"][0]["steps"][0]["follow_ups"] == 1
    assert api.get(f"/projects/1/hospitals/{SNUH}", headers=STRANGER).status_code == 404


def test_api_admin_results(api):
    listed = api.get("/admin/pipeline/results", headers=ADMIN)
    assert listed.status_code == 200 and listed.json()[0]["key"] == "approved"
    assert api.get("/admin/pipeline/results", headers=OWNER).status_code == 403
    r = api.post("/admin/pipeline/results", headers=ADMIN, json={"label": "보류"})
    assert r.status_code == 201
    key = r.json()["key"]
    r = api.patch(f"/admin/pipeline/results/{key}", headers=ADMIN, json={"needs_answer": True})
    assert r.json()["needs_answer"] is True
    assert api.post(f"/admin/pipeline/results/{key}/archive", headers=ADMIN).json()["archived_at"]
    assert api.post(f"/admin/pipeline/results/{key}/restore", headers=ADMIN).json()["archived_at"] is None
    r = api.post("/admin/pipeline/step-types", headers=ADMIN, json={"name": "재심의", "category": "follow_up", "numbered": True})
    assert r.status_code == 201 and (r.json()["category"], r.json()["numbered"]) == ("follow_up", True)
    assert api.post("/admin/pipeline/step-types", headers=ADMIN, json={"name": "Y", "category": "weird"}).status_code == 422
