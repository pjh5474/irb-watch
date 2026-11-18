"""IRB pipeline phase C2: copy a step to sibling sites (offline)."""

from dataclasses import replace

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
from irb_watch.services.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from irb_watch.services.pipeline_admin import DocumentKindEdit as K
from irb_watch.services.pipelines import StepDocument
from irb_watch.services.step_copy import _merge
from tests.test_admin_users_api import FakeVerifier
from tests.test_pipeline_steps import AMENDMENT, EWHA, INITIAL, SNUH, Env
from tests.test_pipelines import NoDrafts


class FakeSessions:
    """services.sessions.SessionService, the parts the pipeline service uses."""

    def __init__(self, valid: set[int]) -> None:
        self.valid = valid

    def check_target(self, session_id, hospital_id):
        if session_id not in self.valid:
            raise ValidationError("this session is cancelled; pick another one")
        from types import SimpleNamespace

        return SimpleNamespace(meets_on="2026-12-10")

    def upcoming_for(self, hospital_id):
        return [], None

    def get_many(self, ids):
        return {}

    def kinds(self, include_archived=False):
        return []


class CopyEnv(Env):
    """Project 1 at SNUH with three PIs (A, B, C) and a PI at Ewha. A, B and the Ewha PI have
    pipelines; C has none."""

    def __init__(self) -> None:
        super().__init__()
        self.setup.save_document_kinds("admin", INITIAL, None, [
            K("신청서", "application", "draft", True),
            K("연구계획서", "submission", "library", True),
            K("ICF", "submission", "upload", True),
        ])
        self.a = self.sstore.create_site(1, SNUH, "owner", "01", "김", "")
        self.b = self.sstore.create_site(1, SNUH, "owner", "02", "이", "")
        self.c = self.sstore.create_site(1, SNUH, "owner", "03", "박", "")
        self.e = self.sstore.create_site(1, EWHA, "owner", "04", "최", "")
        self.pa = self.service.start("owner", 1, site_id=self.a.id)
        self.pb = self.service.start("owner", 1, site_id=self.b.id)
        self.pe = self.service.start("owner", 1, site_id=self.e.id)
        # The fake function's closed statuses follow the setup, as the SQL reads step_statuses.
        self.pstore.closed_statuses = {s.key for s in self.setup.statuses(include_archived=True) if s.closed}

    def step(self, pipeline, type_id):
        return next(
            s for s in self.pstore.steps([pipeline.id])
            if s.step_type_id == type_id and s.deleted_at is None and s.parent_step_id is None
        )

    def docs(self, step):
        return sorted(
            (d for d in self.pstore.documents([step.id]) if d.deleted_at is None), key=lambda d: (d.section, d.position)
        )

    def upload(self, pipeline, step, name="ICF"):
        return self.service.upload("owner", pipeline.id, step.id, name, f"{name}.txt", f"{name} {step.id}".encode())


@pytest.fixture
def env():
    return CopyEnv()


def by_site(targets):
    return {t.site.id: t for t in targets}


# -- the preview ---------------------------------------------------------------------------------


def test_siblings_are_the_other_sites_of_the_same_hospital(env):
    src = env.step(env.pa, INITIAL)
    targets = by_site(env.service.copy_targets("owner", env.pa.id, src.id))
    assert set(targets) == {env.b.id, env.c.id}  # not A itself, not the Ewha site
    assert targets[env.b.id].action == "fill"
    assert targets[env.b.id].step_id == env.step(env.pb, INITIAL).id
    assert (targets[env.c.id].action, targets[env.c.id].reason) == ("skip", "no_pipeline")
    # Nothing was written by the preview.
    assert env.pstore.copy_calls == 0


def test_a_removed_site_or_pipeline_is_not_a_target(env):
    src = env.step(env.pa, INITIAL)
    env.sstore.rows[env.c.id] = replace(env.c, deleted_at="2026-11-19T00:00:00+00:00")
    env.service.remove("owner", env.pb.id)
    targets = by_site(env.service.copy_targets("owner", env.pa.id, src.id))
    assert set(targets) == {env.b.id}
    assert targets[env.b.id].reason == "no_pipeline"


# -- filling the untouched step ------------------------------------------------------------------


def test_copy_fills_the_untouched_step_with_the_layout_and_empty_fields_only(env):
    src = env.step(env.pa, INITIAL)
    env.service.update_step("owner", env.pa.id, src.id, {"subject": "PT 2.0", "due_on": "2026-12-01", "note": "mine"})
    env.service.add_document("owner", env.pa.id, src.id, "모집문건", "submission")
    icf = next(d for d in env.docs(src) if d.name == "ICF")
    env.service.update_document("owner", env.pa.id, src.id, icf.id, {"status": "not_needed"})
    env.upload(env.pa, src, "연구계획서")  # files never come along
    target = env.step(env.pb, INITIAL)
    env.service.update_step("owner", env.pb.id, target.id, {"subject": "B's own"})

    out = env.service.copy_step("owner", env.pa.id, src.id, [env.b.id])

    assert [(t.site.id, t.action, t.step_id) for t in out] == [(env.b.id, "fill", target.id)]
    filled = env.pstore.get_step(target.id)
    assert filled.copied_from_step_id == src.id
    assert filled.subject == "B's own"  # typed there: kept
    assert filled.due_on == "2026-12-01"  # empty there: copied
    assert filled.note == ""  # notes never come along
    docs = {d.name: d for d in env.docs(filled)}
    assert set(docs) == {"신청서", "연구계획서", "ICF", "모집문건"}
    assert docs["ICF"].status == "not_needed"
    assert docs["연구계획서"].status == "needed" and not docs["연구계획서"].versions
    assert docs["모집문건"].listed is False and docs["모집문건"].copied_from_document_id is not None
    assert docs["ICF"].copied_from_document_id == icf.id
    # History on both sides.
    kinds = [e.event_type for e in env.pstore.event_rows[-2:]]
    assert kinds == ["step.copied_in", "step.copied"]


def test_a_second_copy_to_the_same_site_is_skipped(env):
    src = env.step(env.pa, INITIAL)
    env.service.copy_step("owner", env.pa.id, src.id, [env.b.id])
    again = by_site(env.service.copy_targets("owner", env.pa.id, src.id))[env.b.id]
    assert (again.action, again.reason) == ("skip", "already_copied")
    with pytest.raises(ConflictError, match="nothing was copied: 02 · PI 이: already has a copy"):
        env.service.copy_step("owner", env.pa.id, src.id, [env.b.id])


def test_a_sibling_step_with_work_is_skipped_for_a_type_that_is_not_numbered(env):
    src = env.step(env.pa, INITIAL)
    env.upload(env.pb, env.step(env.pb, INITIAL))
    target = by_site(env.service.copy_targets("owner", env.pa.id, src.id))[env.b.id]
    assert (target.action, target.reason) == ("skip", "has_work")
    with pytest.raises(ConflictError, match="its step of this type already has work"):
        env.service.copy_step("owner", env.pa.id, src.id, [env.b.id])
    assert env.pstore.copy_calls == 0


@pytest.mark.parametrize("change", [
    {"status_key": "submitted"}, {"decided_on": "2026-11-01"}, {"result_key": "approved"},
])
def test_a_submitted_decided_or_answered_step_is_not_untouched(env, change):
    src = env.step(env.pa, INITIAL)
    env.service.update_step("owner", env.pb.id, env.step(env.pb, INITIAL).id, change)
    assert by_site(env.service.copy_targets("owner", env.pa.id, src.id))[env.b.id].reason == "has_work"


def test_a_numbered_type_gets_a_new_step_with_the_next_number_when_none_is_untouched(env):
    env.upload(env.pb, env.step(env.pb, AMENDMENT))  # B's 1차 변경심의 has work
    src = env.service.add_step("owner", env.pa.id, None, AMENDMENT, subject="PT 3.0")  # A's 2차
    target = by_site(env.service.copy_targets("owner", env.pa.id, src.id))[env.b.id]
    assert (target.action, target.step_label) == ("add", "2차 변경 심의")
    out = env.service.copy_step("owner", env.pa.id, src.id, [env.b.id])
    made = env.pstore.get_step(out[0].step_id)
    assert (made.pipeline_id, made.sequence_no, made.subject, made.copied_from_step_id) == (env.pb.id, 2, "PT 3.0", src.id)
    assert made.status_key == "not_started"


def test_a_numbered_type_fills_the_lowest_untouched_step(env):
    src = env.step(env.pa, AMENDMENT)
    second = env.service.add_step("owner", env.pb.id, None, AMENDMENT)  # B has 1차 and 2차, both untouched
    target = by_site(env.service.copy_targets("owner", env.pa.id, src.id))[env.b.id]
    assert (target.action, target.step_id) == ("fill", env.step(env.pb, AMENDMENT).id)
    assert target.step_id != second.id


@pytest.mark.parametrize("type_id", [INITIAL, AMENDMENT])
def test_an_untouched_copy_of_another_sites_step_is_skipped_not_doubled(type_id):
    env = CopyEnv()
    pc = env.service.start("owner", 1, site_id=env.c.id)
    env.service.copy_step("owner", env.pa.id, env.step(env.pa, type_id).id, [env.c.id])  # A -> C
    target = by_site(env.service.copy_targets("owner", env.pb.id, env.step(env.pb, type_id).id))[env.c.id]
    assert (target.action, target.reason, target.step_id) == ("skip", "copied_from_other", env.step(pc, type_id).id)
    with pytest.raises(ConflictError, match="already a copy of another site's step"):
        env.service.copy_step("owner", env.pb.id, env.step(env.pb, type_id).id, [env.c.id])  # B -> C


def test_a_copy_with_work_still_gets_a_new_round_for_a_numbered_type(env):
    pc = env.service.start("owner", 1, site_id=env.c.id)
    env.service.copy_step("owner", env.pa.id, env.step(env.pa, AMENDMENT).id, [env.c.id])
    env.upload(pc, env.step(pc, AMENDMENT))  # C's copy now has work
    target = by_site(env.service.copy_targets("owner", env.pb.id, env.step(env.pb, AMENDMENT).id))[env.c.id]
    assert (target.action, target.step_label) == ("add", "2차 변경 심의")


def test_the_fake_function_refuses_a_step_answered_or_closed_after_planning(env, monkeypatch):
    src = env.step(env.pa, INITIAL)
    real = env.pstore.copy_step

    def close_first(*args):
        target = env.step(env.pb, INITIAL)
        env.pstore.step_rows[target.id] = replace(target, status_key="approved")
        return real(*args)

    monkeypatch.setattr(env.pstore, "copy_step", close_first)
    with pytest.raises(ConflictError, match="changed meanwhile"):
        env.service.copy_step("owner", env.pa.id, src.id, [env.b.id])


def test_a_free_step_is_always_added_and_a_follow_up_is_never_copied(env):
    free = env.service.add_step("owner", env.pa.id, "사전 협의", None)
    out = env.service.copy_step("owner", env.pa.id, free.id, [env.b.id])
    assert out[0].action == "add"
    assert env.pstore.get_step(out[0].step_id).name == "사전 협의"
    follow = env.service.add_step("owner", env.pa.id, None, 7, parent_step_id=env.step(env.pa, INITIAL).id)
    with pytest.raises(ValidationError, match="follow-up"):
        env.service.copy_targets("owner", env.pa.id, follow.id)


def test_the_target_meeting_comes_along_only_while_it_is_valid():
    env = CopyEnv()
    env.service._sessions = FakeSessions({50})
    src = env.step(env.pa, INITIAL)
    env.pstore.step_rows[src.id] = replace(src, target_session_id=50, decided_on="2026-12-10")  # the meeting's day
    env.service.copy_step("owner", env.pa.id, src.id, [env.b.id])
    copy = env.step(env.pb, INITIAL)
    assert (copy.target_session_id, copy.decided_on) == (50, "2026-12-10")
    env.service._sessions = FakeSessions(set())  # cancelled meanwhile
    free = env.service.add_step("owner", env.pa.id, "협의", None)
    env.pstore.step_rows[free.id] = replace(env.pstore.step_rows[free.id], target_session_id=50)
    out = env.service.copy_step("owner", env.pa.id, free.id, [env.b.id])
    assert out[0].notes == ("meeting_not_copied",)
    assert env.pstore.get_step(out[0].step_id).target_session_id is None


def test_a_decision_date_that_is_not_the_meeting_day_does_not_go_along():
    env = CopyEnv()
    env.service._sessions = FakeSessions({50})
    src = env.step(env.pa, INITIAL)
    env.pstore.step_rows[src.id] = replace(src, target_session_id=50, decided_on="2026-12-15")  # typed or real
    env.service.copy_step("owner", env.pa.id, src.id, [env.b.id])
    copy = env.step(env.pb, INITIAL)
    assert (copy.target_session_id, copy.decided_on) == (50, None)


def slot(sid, name, kind=None, section="submission", position=1, status="needed", step=1):
    return StepDocument(sid, step, name, "2026-11-16T00:00:00+00:00", kind_id=kind, section=section,
                        position=position, status=status, listed=kind is not None)


def test_a_siblings_own_not_needed_mark_is_never_taken_away(env):
    src = env.step(env.pa, INITIAL)
    target = env.step(env.pb, INITIAL)
    icf = next(d for d in env.docs(target) if d.name == "ICF")
    env.service.update_document("owner", env.pb.id, target.id, icf.id, {"status": "not_needed"})
    env.service.copy_step("owner", env.pa.id, src.id, [env.b.id])  # A's ICF is still needed
    assert next(d for d in env.docs(target) if d.name == "ICF").status == "not_needed"


def test_a_renamed_list_row_never_plans_a_name_the_target_has():
    # Target: list row 5 still named "B". Source: row 5 renamed "A", plus the user's own "B".
    source = [slot(1, "A", kind=5, position=1), slot(2, "B", position=2)]
    target = [slot(10, "B", kind=5, position=1, step=2)]
    updates, inserts = _merge(source, target)
    assert inserts == []  # "B" exists at the target: kept, not inserted again
    assert updates == [{"id": 10, "copied_from_document_id": 1, "position": 1}]


def test_a_name_match_in_another_section_goes_last_in_its_own_section():
    source = [slot(1, "통지서", section="submission", position=2, status="not_needed")]
    target = [slot(10, "통지서", section="reference", position=1, step=2), slot(11, "Other", position=1, step=2)]
    updates, inserts = _merge(source, target)
    assert inserts == []
    assert updates == [
        {"id": 10, "position": 3, "copied_from_document_id": 1, "status": "not_needed"},
        {"id": 11, "position": 4},
    ]


def test_a_removed_source_site_shows_as_removed(env):
    src = env.step(env.pa, INITIAL)
    env.service.copy_step("owner", env.pa.id, src.id, [env.b.id])
    env.sstore.rows[env.a.id] = replace(env.a, deleted_at="2026-11-19T00:00:00+00:00")
    assert env.service.step_page("owner", env.pb.id, env.step(env.pb, INITIAL).id).copied_from.removed is True


def test_the_history_row_at_the_site_names_the_source(env):
    src = env.step(env.pa, INITIAL)
    env.service.copy_step("owner", env.pa.id, src.id, [env.b.id])
    copied_in = next(e for e in env.pstore.event_rows if e.event_type == "step.copied_in")
    assert copied_in.payload == {"from_pipeline_id": env.pa.id, "from_step_id": src.id, "mode": "fill", "name": src.name}


# -- rules and races -----------------------------------------------------------------------------


def test_copying_needs_edit_access_and_a_sibling_of_the_hospital(env):
    src = env.step(env.pa, INITIAL)
    with pytest.raises(NotFoundError):
        env.service.copy_targets("stranger", env.pa.id, src.id)
    with pytest.raises(ForbiddenError):
        env.service.copy_targets("admin", env.pa.id, src.id)  # platform admins read only
    with pytest.raises(ValidationError, match="other sites of this hospital"):
        env.service.copy_step("owner", env.pa.id, src.id, [env.e.id])  # Ewha: another IRB
    with pytest.raises(ValidationError, match="at least one"):
        env.service.copy_step("owner", env.pa.id, src.id, [])
    env.service.remove_step("owner", env.pa.id, src.id)
    with pytest.raises(ConflictError, match="removed"):
        env.service.copy_targets("owner", env.pa.id, src.id)


def test_skipped_sites_are_named_next_to_the_copied_ones(env):
    src = env.step(env.pa, INITIAL)
    out = env.service.copy_step("owner", env.pa.id, src.id, [env.b.id, env.c.id])
    assert [(t.site.id, t.action, t.reason) for t in out] == [(env.b.id, "fill", None), (env.c.id, "skip", "no_pipeline")]
    assert env.pstore.event_rows[-1].payload["skipped"] == [{"site_id": env.c.id, "reason": "no_pipeline"}]


def test_a_change_meanwhile_writes_nothing_and_says_why(env):
    src = env.step(env.pa, INITIAL)
    before = (dict(env.pstore.step_rows), dict(env.pstore.docs), len(env.pstore.event_rows))
    env.pstore.copy_refusal = "a step at one of the sites changed meanwhile"
    with pytest.raises(ConflictError, match="changed meanwhile; nothing was copied"):
        env.service.copy_step("owner", env.pa.id, src.id, [env.b.id])
    assert (dict(env.pstore.step_rows), dict(env.pstore.docs), len(env.pstore.event_rows)) == before


def test_the_fake_function_refuses_a_step_that_got_a_file_after_planning(env, monkeypatch):
    src = env.step(env.pa, INITIAL)
    real = env.pstore.copy_step

    def upload_first(*args):
        env.upload(env.pb, env.step(env.pb, INITIAL))
        return real(*args)

    monkeypatch.setattr(env.pstore, "copy_step", upload_first)
    with pytest.raises(ConflictError, match="changed meanwhile"):
        env.service.copy_step("owner", env.pa.id, src.id, [env.b.id])
    assert env.step(env.pb, INITIAL).copied_from_step_id is None


def test_restoring_a_second_live_copy_is_a_clear_conflict(env):
    src = env.step(env.pa, INITIAL)
    env.upload(env.pb, env.step(env.pb, INITIAL))
    first = env.service.copy_step("owner", env.pa.id, env.service.add_step("owner", env.pa.id, "협의", None).id, [env.b.id])
    copy = env.pstore.get_step(first[0].step_id)
    env.service.remove_step("owner", env.pb.id, copy.id)
    env.service.copy_step("owner", env.pa.id, copy.copied_from_step_id, [env.b.id])  # a new live copy
    with pytest.raises(ConflictError, match="copy of the same step"):
        env.service.restore_step("owner", env.pb.id, copy.id)
    assert src.id  # the main step stays untouched


# -- the step page -------------------------------------------------------------------------------


def test_the_step_page_says_where_a_copy_came_from(env):
    src = env.step(env.pa, INITIAL)
    env.service.copy_step("owner", env.pa.id, src.id, [env.b.id])
    page = env.service.step_page("owner", env.pb.id, env.step(env.pb, INITIAL).id)
    assert page.copied_from is not None
    assert (page.copied_from.pipeline_id, page.copied_from.step_id, page.copied_from.site_label) == (
        env.pa.id, src.id, "01 · PI 김",
    )
    assert page.copied_from.removed is False
    env.service.remove_step("owner", env.pa.id, src.id)
    assert env.service.step_page("owner", env.pb.id, env.step(env.pb, INITIAL).id).copied_from.removed is True
    assert env.service.step_page("owner", env.pa.id, env.step(env.pa, AMENDMENT).id).copied_from is None


# -- the API -------------------------------------------------------------------------------------


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


def test_the_routes(env, api):
    src = env.step(env.pa, INITIAL)
    base = f"/pipelines/{env.pa.id}/steps/{src.id}"
    preview = api.get(f"{base}/copy-targets", headers=OWNER)
    assert preview.status_code == 200
    rows = {r["site_id"]: r for r in preview.json()}
    assert rows[env.b.id]["action"] == "fill" and rows[env.b.id]["pi_name"] == "이"
    assert rows[env.c.id]["reason"] == "no_pipeline"
    done = api.post(f"{base}/copy", json={"site_ids": [env.b.id]}, headers=OWNER)
    assert done.status_code == 200, done.text
    assert done.json()[0]["action"] == "fill"
    again = api.post(f"{base}/copy", json={"site_ids": [env.b.id]}, headers=OWNER)
    assert again.status_code == 409 and "already has a copy" in again.json()["detail"]
    assert api.post(f"{base}/copy", json={"site_ids": []}, headers=OWNER).status_code == 422
    page = api.get(f"/pipelines/{env.pb.id}/steps/{env.step(env.pb, INITIAL).id}", headers=OWNER).json()
    assert page["copied_from"]["step_id"] == src.id
    assert page["step"]["copied_from_step_id"] == src.id
