"""IRB pipeline phase 2: users' pipelines, steps, documents, history and purge (offline)."""

import base64
import io
from datetime import UTC, datetime, timedelta

import pytest
from docx import Document
from fastapi.testclient import TestClient

from irb_watch.api.deps import (
    get_docgen_service,
    get_draft_job_service,
    get_pipeline_service,
    get_user_store,
    get_verifier,
)
from irb_watch.main import app
from irb_watch.services.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UpstreamError,
    ValidationError,
)
from irb_watch.services.pipeline_admin import PipelineSetupService
from irb_watch.services.pipelines import PipelineService, safe_filename
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_pipeline import FakePipelineSetupStore
from tests.fakes_pipelines import FakePipelineFiles, FakePipelineStore
from tests.test_admin_users_api import FakeVerifier

# 2026-11-20 23:30 UTC is already 2026-11-21 in Seoul.
NOW = datetime(2026, 11, 20, 23, 30, tzinfo=UTC)


def docx_bytes(text: str) -> bytes:
    doc = Document()
    for _ in range(20):  # the reader treats a nearly empty file as "no text"
        doc.add_paragraph(text)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


def make():
    users = FakeStore()
    users.add("u1", "a@example.com")
    users.add("u2", "b@example.com")
    users.add("gone", "g@example.com", deleted_at="2026-11-01T00:00:00+00:00")
    setup_store = FakePipelineSetupStore()
    setup = PipelineSetupService(setup_store, FakeAudit())
    store = FakePipelineStore()
    store.add_project(1, "u1", {4, 6})
    store.add_project(2, "u2", {4})
    files = FakePipelineFiles()
    clock = Clock()
    service = PipelineService(users, store, setup, files, max_file_bytes=200_000, clock=clock)
    return service, store, setup, setup_store, files, clock


# -- starting ----------------------------------------------------------------------------------


def test_start_copies_the_global_order_with_the_first_open_status():
    service, store, *_ = make()
    pipeline = service.start("u1", 1, 4)
    steps = sorted(store.steps([pipeline.id]), key=lambda s: s.position)
    assert [s.name for s in steps] == ["초기 심의", "변경 심의", "지속 심의", "종료 보고"]
    assert {s.status_key for s in steps} == {"not_started"}
    assert store.event_rows[-1].event_type == "pipeline.started"
    assert store.event_rows[-1].payload["from"] == "global"


def test_start_uses_the_hospitals_own_order_and_skips_archived_types():
    service, store, setup, setup_store, *_ = make()
    setup.save_template("admin", 4, [1, 5, 6])
    setup_store.types[5] = setup_store.types[5].__class__(**{**setup_store.types[5].__dict__, "archived_at": "x"})
    pipeline = service.start("u1", 1, 4)
    assert [s.name for s in sorted(store.steps([pipeline.id]), key=lambda s: s.position)] == ["초기 심의", "종료 보고"]


def test_start_rules():
    service, store, *_ = make()
    with pytest.raises(NotFoundError, match="project"):
        service.start("u2", 1, 4)  # not the owner
    with pytest.raises(ValidationError, match="not in the project"):
        service.start("u1", 1, 5)
    # Any hospital in the project may get a pipeline, readable boards or not (owner, 2026-11-12).
    service.start("u1", 1, 6)
    service.start("u1", 1, 4)
    with pytest.raises(ConflictError, match="already has a pipeline"):
        service.start("u1", 1, 4)
    with pytest.raises(ForbiddenError):
        service.start("gone", 1, 4)


def test_list_and_detail_are_owner_only_and_show_hospitals_removed_from_the_project():
    service, store, *_ = make()
    pipeline = service.start("u1", 1, 4)
    with pytest.raises(NotFoundError):
        service.detail("u2", pipeline.id)
    with pytest.raises(NotFoundError):
        service.list_for_project("u2", 1)
    store.project_hospitals[1].discard(4)
    [summary] = service.list_for_project("u1", 1)
    assert summary.in_project is False and summary.hospital_name == "SNUH" and summary.steps == 4
    detail = service.detail("u1", pipeline.id)
    assert len(detail.steps) == 4 and detail.step_types


def test_remove_and_restore_pipeline():
    service, *_ = make()
    pipeline = service.start("u1", 1, 4)
    service.remove("u1", pipeline.id)
    assert service.list_for_project("u1", 1) == []
    assert len(service.list_for_project("u1", 1, include_removed=True)) == 1
    with pytest.raises(ConflictError, match="removed"):
        service.add_step("u1", pipeline.id, "x", None)
    newer = service.start("u1", 1, 4)
    with pytest.raises(ConflictError, match="newer pipeline"):
        service.restore("u1", pipeline.id)
    service.remove("u1", newer.id)
    assert service.restore("u1", pipeline.id).deleted_at is None


# -- steps -------------------------------------------------------------------------------------


def test_add_steps_from_a_type_or_free():
    service, store, *_ = make()
    pipeline = service.start("u1", 1, 4)
    typed = service.add_step("u1", pipeline.id, None, 2)
    assert typed.name == "변경 심의" and typed.position == 5 and typed.step_type_id == 2
    renamed = service.add_step("u1", pipeline.id, " 2차   변경 ", 2)
    assert renamed.name == "2차 변경"
    free = service.add_step("u1", pipeline.id, "IRB 회의 참석", None)
    assert free.step_type_id is None
    with pytest.raises(ValidationError):
        service.add_step("u1", pipeline.id, "", None)
    with pytest.raises(ValidationError, match="step type"):
        service.add_step("u1", pipeline.id, None, 99)


def test_status_change_fills_dates_in_seoul_time():
    service, store, *_ = make()
    pipeline = service.start("u1", 1, 4)
    step = min(store.steps([pipeline.id]), key=lambda s: s.position)
    submitted = service.update_step("u1", pipeline.id, step.id, {"status_key": "submitted"})
    assert submitted.submitted_on == "2026-11-21" and submitted.decided_on is None
    approved = service.update_step("u1", pipeline.id, step.id, {"status_key": "approved"})
    assert approved.decided_on == "2026-11-21"
    event = store.event_rows[-1]
    assert event.payload["status_key"] == {"from": "submitted", "to": "approved"}


def test_dates_sent_with_the_status_win_and_can_be_cleared():
    service, store, *_ = make()
    pipeline = service.start("u1", 1, 4)
    step = min(store.steps([pipeline.id]), key=lambda s: s.position)
    s = service.update_step("u1", pipeline.id, step.id, {"status_key": "submitted", "submitted_on": "2026-11-02"})
    assert s.submitted_on == "2026-11-02"
    s = service.update_step("u1", pipeline.id, step.id, {"submitted_on": None, "due_on": "2026-12-31", "note": " hi "})
    assert s.submitted_on is None and s.due_on == "2026-12-31" and s.note == "hi"
    with pytest.raises(ValidationError, match="2026-11-30"):
        service.update_step("u1", pipeline.id, step.id, {"due_on": "31/12/2026"})
    with pytest.raises(ValidationError, match="status"):
        service.update_step("u1", pipeline.id, step.id, {"status_key": "nope"})
    count = len(store.event_rows)
    service.update_step("u1", pipeline.id, step.id, {"due_on": "2026-12-31"})  # nothing changes
    assert len(store.event_rows) == count


def test_reorder_remove_and_restore_steps():
    service, store, *_ = make()
    pipeline = service.start("u1", 1, 4)
    ids = [s.id for s in sorted(store.steps([pipeline.id]), key=lambda s: s.position)]
    reordered = service.reorder_steps("u1", pipeline.id, ids[::-1])
    assert [s.id for s in reordered] == ids[::-1]
    with pytest.raises(ValidationError):
        service.reorder_steps("u1", pipeline.id, ids[:-1])
    service.remove_step("u1", pipeline.id, ids[0])
    with pytest.raises(ValidationError):
        service.reorder_steps("u1", pipeline.id, ids)  # the removed one may not be sent
    with pytest.raises(ConflictError):
        service.update_step("u1", pipeline.id, ids[0], {"name": "x"})
    restored = service.restore_step("u1", pipeline.id, ids[0])
    live = [s for s in store.steps([pipeline.id]) if s.deleted_at is None]
    assert restored.deleted_at is None and restored.position == max(s.position for s in live)  # goes last
    assert len({s.position for s in live}) == len(live)


# -- step page (phase A) -----------------------------------------------------------------------


def test_step_page_has_its_documents_and_neighbours_in_overview_order():
    service, store, _, setup_store, *_ = make()
    setup_store.types[1] = setup_store.types[1].__class__(
        **{**setup_store.types[1].__dict__, "required_documents": ("연구계획서", "동의서")}
    )
    pipeline = service.start("u1", 1, 4)
    ids = [s.id for s in sorted(store.steps([pipeline.id]), key=lambda s: s.position)]
    service.upload("u1", pipeline.id, ids[0], "연구계획서", "p.xlsx", b"abc")
    service.upload("u1", pipeline.id, ids[1], "Other step", "o.xlsx", b"abc")

    page = service.step_page("u1", pipeline.id, ids[0])
    assert [d.name for d in page.view.documents] == ["연구계획서"]  # this step's only
    assert (page.view.documents_with_files, page.view.required_missing) == (1, 1)
    assert (page.previous_step_id, page.next_step_id) == (None, ids[1])
    assert page.summary.hospital_name == "SNUH" and page.statuses

    service.remove_step("u1", pipeline.id, ids[1])  # removed steps are skipped
    page = service.step_page("u1", pipeline.id, ids[2])
    assert (page.previous_step_id, page.next_step_id) == (ids[0], ids[3])
    removed = service.step_page("u1", pipeline.id, ids[1])  # still opens, read-only in the page
    assert removed.view.step.deleted_at is not None
    assert (removed.previous_step_id, removed.next_step_id) == (None, None)


def test_step_page_of_a_removed_pipeline_opens_and_strangers_get_not_found():
    service, store, *_ = make()
    pipeline = service.start("u1", 1, 4)
    other = service.start("u2", 2, 4)
    step_id = min(store.steps([pipeline.id]), key=lambda s: s.position).id
    service.remove("u1", pipeline.id)
    page = service.step_page("u1", pipeline.id, step_id)
    assert page.summary.pipeline.deleted_at is not None
    with pytest.raises(NotFoundError):
        service.step_page("u2", pipeline.id, step_id)
    other_step = min(store.steps([other.id]), key=lambda s: s.position).id
    with pytest.raises(NotFoundError, match="step not found"):
        service.step_page("u1", pipeline.id, other_step)  # a step of another pipeline
    with pytest.raises(NotFoundError, match="step not found"):
        service.step_page("u1", pipeline.id, 99999)


def test_history_of_one_step():
    service, store, *_ = make()
    pipeline = service.start("u1", 1, 4)
    first, second = sorted(store.steps([pipeline.id]), key=lambda s: s.position)[:2]
    service.update_step("u1", pipeline.id, first.id, {"note": "a"})
    service.update_step("u1", pipeline.id, second.id, {"note": "b"})
    events = service.history("u1", pipeline.id, step_id=first.id)
    assert events and {e.step_id for e in events} == {first.id}
    assert len(service.history("u1", pipeline.id)) > len(events)
    with pytest.raises(NotFoundError):
        service.history("u1", pipeline.id, step_id=99999)


# -- documents ---------------------------------------------------------------------------------


def test_upload_versions_by_name_and_reads_text():
    service, store, _, _, files, _ = make()
    pipeline = service.start("u1", 1, 4)
    step = min(store.steps([pipeline.id]), key=lambda s: s.position)
    doc, v1 = service.upload("u1", pipeline.id, step.id, "연구계획서", "protocol v1.docx", docx_bytes("Protocol one"))
    assert v1.version_no == 1 and v1.has_text and store.texts[v1.id].startswith("Protocol one")
    # The same bytes twice: a DOCX built again carries a new timestamp and would not be equal.
    two = docx_bytes("Protocol two")
    doc2, v2 = service.upload("u1", pipeline.id, step.id, " 연구계획서 ", "protocol v2.docx", two)
    assert doc2.id == doc.id and v2.version_no == 2
    with pytest.raises(ConflictError, match="same as version 2"):
        service.upload("u1", pipeline.id, step.id, "연구계획서", "again.docx", two)
    assert len(files.stored) == 2
    assert all(path.startswith(f"p{pipeline.id}/s{step.id}/") for path in files.stored)


def test_upload_refusals_and_files_without_text():
    service, store, _, _, files, _ = make()
    pipeline = service.start("u1", 1, 4)
    step = min(store.steps([pipeline.id]), key=lambda s: s.position)
    with pytest.raises(ValidationError, match="not the kind of file"):
        service.upload("u1", pipeline.id, step.id, "x", "fake.pdf", b"not a pdf")
    with pytest.raises(ValidationError, match="larger than"):
        service.upload("u1", pipeline.id, step.id, "x", "big.xlsx", b"x" * 200_001)
    with pytest.raises(ValidationError, match="empty"):
        service.upload("u1", pipeline.id, step.id, "x", "e.xlsx", b"")
    _, v = service.upload("u1", pipeline.id, step.id, "Budget", "budget.xlsx", b"PK\x03\x04 sheet")
    assert v.has_text is False and v.text_note
    assert list(files.stored) == [v.storage_path]


def test_storage_failures_leave_nothing_behind():
    service, store, _, _, files, _ = make()
    pipeline = service.start("u1", 1, 4)
    step = min(store.steps([pipeline.id]), key=lambda s: s.position)
    files.fail_put = True
    with pytest.raises(UpstreamError, match="nothing was saved"):
        service.upload("u1", pipeline.id, step.id, "A", "a.xlsx", b"abc")
    assert store.docs == {}
    files.fail_put = False
    store.fail_version = True
    with pytest.raises(RuntimeError):
        service.upload("u1", pipeline.id, step.id, "A", "a.xlsx", b"abc")
    assert files.stored == {} and len(files.deleted) == 1


def test_checklist_follows_live_documents_with_versions():
    service, store, _, setup_store, *_ = make()
    setup_store.types[1] = setup_store.types[1].__class__(
        **{**setup_store.types[1].__dict__, "required_documents": ("연구계획서", "동의서")}
    )
    pipeline = service.start("u1", 1, 4)
    step = min(store.steps([pipeline.id]), key=lambda s: s.position)
    doc, _ = service.upload("u1", pipeline.id, step.id, "연구계획서", "p.xlsx", b"abc")
    view = service.detail("u1", pipeline.id).steps[0]
    assert [(c.name, c.uploaded) for c in view.checklist] == [("연구계획서", True), ("동의서", False)]
    service.remove_document("u1", pipeline.id, step.id, doc.id)
    view = service.detail("u1", pipeline.id).steps[0]
    assert view.checklist[0].uploaded is False


def test_remove_restore_document_and_name_clash():
    service, store, *_ = make()
    pipeline = service.start("u1", 1, 4)
    step = min(store.steps([pipeline.id]), key=lambda s: s.position)
    doc, _ = service.upload("u1", pipeline.id, step.id, "A", "a.xlsx", b"abc")
    service.remove_document("u1", pipeline.id, step.id, doc.id)
    service.upload("u1", pipeline.id, step.id, "a", "b.xlsx", b"def")  # same name: a new document
    with pytest.raises(ConflictError, match="exists in the step"):
        service.restore_document("u1", pipeline.id, step.id, doc.id)


def test_download_link_is_owner_only_and_logged():
    service, store, *_ = make()
    pipeline = service.start("u1", 1, 4)
    step = min(store.steps([pipeline.id]), key=lambda s: s.position)
    doc, v = service.upload("u1", pipeline.id, step.id, "A", "계획서 v1.xlsx", b"abc")
    url = service.download_url("u1", pipeline.id, step.id, doc.id, 1)
    assert "download=계획서 v1.xlsx" in url and "ttl=60" in url
    assert store.event_rows[-1].event_type == "document.downloaded"
    with pytest.raises(NotFoundError):
        service.download_url("u2", pipeline.id, step.id, doc.id, 1)
    with pytest.raises(NotFoundError):
        service.download_url("u1", pipeline.id, step.id, doc.id, 9)


def test_safe_filename():
    assert safe_filename("..\\..\\계획서 v1.docx") == "v1.docx"
    assert safe_filename("a b/c d.PDF") == "c_d.PDF"
    assert safe_filename("noext") == "noext"
    assert safe_filename("....") == "file"


# -- purge -------------------------------------------------------------------------------------


def test_purge_removes_old_removed_things_and_their_files():
    service, store, _, _, files, clock = make()
    pipeline = service.start("u1", 1, 4)
    steps = sorted(store.steps([pipeline.id]), key=lambda s: s.position)
    d1, v1 = service.upload("u1", pipeline.id, steps[0].id, "A", "a.xlsx", b"1")
    d2, v2 = service.upload("u1", pipeline.id, steps[1].id, "B", "b.xlsx", b"2")
    _, v3 = service.upload("u1", pipeline.id, steps[2].id, "C", "c.xlsx", b"3")
    service.remove_document("u1", pipeline.id, steps[0].id, d1.id)
    service.remove_step("u1", pipeline.id, steps[1].id)
    assert service.purge_old() == 0  # removed just now
    clock.now = NOW + timedelta(days=31)
    assert service.purge_old() == 2
    assert sorted(files.deleted) == sorted([v1.storage_path, v2.storage_path])
    assert v3.storage_path in files.stored
    service.remove("u1", pipeline.id)
    clock.now = NOW + timedelta(days=62)
    assert service.purge_old() == 1
    assert v3.storage_path in files.deleted and store.pipes == {}


# -- after the code review ---------------------------------------------------------------------


def test_restore_step_keeps_the_step_limit(monkeypatch):
    import irb_watch.services.pipelines as mod

    monkeypatch.setattr(mod, "MAX_STEPS", 4)
    service, store, *_ = make()
    pipeline = service.start("u1", 1, 4)  # 4 steps
    first = min(store.steps([pipeline.id]), key=lambda s: s.position)
    service.remove_step("u1", pipeline.id, first.id)
    service.add_step("u1", pipeline.id, "Extra", None)  # back to 4
    with pytest.raises(ConflictError, match="at most 4 steps"):
        service.restore_step("u1", pipeline.id, first.id)


def test_upload_answer_includes_the_new_version():
    service, store, *_ = make()
    pipeline = service.start("u1", 1, 4)
    step = min(store.steps([pipeline.id]), key=lambda s: s.position)
    doc, _ = service.upload("u1", pipeline.id, step.id, "A", "a.xlsx", b"1")
    assert [v.version_no for v in doc.versions] == [1]
    doc, _ = service.upload("u1", pipeline.id, step.id, "A", "a.xlsx", b"2")
    assert [v.version_no for v in doc.versions] == [2, 1]


def test_simultaneous_upload_is_a_clear_conflict_and_leaves_no_empty_document():
    service, store, _, _, files, _ = make()
    pipeline = service.start("u1", 1, 4)
    step = min(store.steps([pipeline.id]), key=lambda s: s.position)
    store.version_race = True
    with pytest.raises(ConflictError, match="same moment"):
        service.upload("u1", pipeline.id, step.id, "A", "a.xlsx", b"1")
    assert store.docs == {} and files.stored == {}


def test_reorder_is_one_call_and_a_changed_list_is_a_conflict():
    service, store, *_ = make()
    pipeline = service.start("u1", 1, 4)
    ids = [s.id for s in sorted(store.steps([pipeline.id]), key=lambda s: s.position)]
    store.steps_changed_meanwhile = True
    with pytest.raises(ConflictError, match="reload"):
        service.reorder_steps("u1", pipeline.id, ids[::-1])
    assert [s.id for s in sorted(store.steps([pipeline.id]), key=lambda s: s.position)] == ids


def test_a_step_that_vanished_is_not_found_not_a_crash():
    service, store, *_ = make()
    pipeline = service.start("u1", 1, 4)
    step = min(store.steps([pipeline.id]), key=lambda s: s.position)
    real = store.update_step

    def vanish(step_id, values):
        store.step_rows.pop(step_id, None)
        return real(step_id, values)

    store.update_step = vanish
    with pytest.raises(NotFoundError, match="reload"):
        service.update_step("u1", pipeline.id, step.id, {"name": "x"})


def test_purge_never_leaves_files_without_rows():
    service, store, _, _, files, clock = make()
    pipeline = service.start("u1", 1, 4)
    step = min(store.steps([pipeline.id]), key=lambda s: s.position)
    doc, v = service.upload("u1", pipeline.id, step.id, "A", "a.xlsx", b"1")
    service.remove_document("u1", pipeline.id, step.id, doc.id)
    clock.now = NOW + timedelta(days=31)
    files.fail_delete = True  # files cannot be deleted: rows stay, the next run tries again
    assert service.purge_old() == 0
    assert doc.id in store.docs and v.storage_path in files.stored
    files.fail_delete = False
    store.fail_purge_rows = {"documents"}  # files go, rows fail: the next run deletes the rows
    assert service.purge_old() == 0
    assert v.storage_path in files.deleted and doc.id in store.docs
    store.fail_purge_rows = set()
    assert service.purge_old() == 1
    assert doc.id not in store.docs


class _Query:
    def __init__(self, client, table):
        self.client, self.table, self.filters = client, table, {}

    def select(self, *_):
        return self

    def in_(self, column, values):
        self.filters[column] = set(values)
        return self

    def eq(self, column, value):
        self.filters[column] = {value}
        return self

    def execute(self):
        self.client.queries.append(self.table)
        rows = [r for r in self.client.rows[self.table] if all(r.get(k) in v for k, v in self.filters.items())]
        return type("R", (), {"data": rows})()


class _Client:
    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def table(self, name):
        return _Query(self, name)


def test_store_reads_hospitals_of_a_project_and_their_names():
    from irb_watch.db.pipelines import SupabasePipelineStore

    client = _Client(
        {
            "hospitals": [{"id": 4, "name": "SNUH"}, {"id": 6, "name": "Kimpo"}, {"id": 7, "name": "Ewha"}],
            "project_hospitals": [
                {"project_id": 1, "hospital_id": 4}, {"project_id": 1, "hospital_id": 7},
                {"project_id": 2, "hospital_id": 6},
            ],
        }
    )
    store = SupabasePipelineStore(client)
    assert store.project_hospital_ids(1) == {4, 7}
    assert store.hospital_names([4, 6, 6]) == {4: "SNUH", 6: "Kimpo"}
    assert store.hospital_names([]) == {}
    assert client.queries == ["project_hospitals", "hospitals"]


def test_pipeline_upload_is_refused_by_declared_size_before_reading():
    client = TestClient(app)
    big = {"Content-Length": str(30_000_000), "Authorization": "Bearer x"}
    r = client.post("/pipelines/1/steps/2/documents", headers=big, content=b"{}")
    assert r.status_code == 413 and "20 MB" in r.json()["detail"]


# -- API ---------------------------------------------------------------------------------------


class NoDrafts:
    """The overview and the step page also read drafts and jobs; tests/test_step_drafts.py
    covers them with the real services."""

    def step_drafts(self, user_id, step_ids):
        return []

    def list(self, user_id):
        return []


@pytest.fixture
def api():
    service, store, *_ = make()
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: service._users
    app.dependency_overrides[get_pipeline_service] = lambda: service
    app.dependency_overrides[get_docgen_service] = NoDrafts
    app.dependency_overrides[get_draft_job_service] = NoDrafts
    yield TestClient(app), store
    app.dependency_overrides.clear()


U1 = {"Authorization": "Bearer tok-u1"}
U2 = {"Authorization": "Bearer tok-u2"}


def test_api_flow(api):
    client, store = api
    assert client.get("/projects/1/pipelines").status_code == 401
    r = client.post("/projects/1/pipelines", headers=U1, json={"hospital_id": 4})
    assert r.status_code == 201 and r.json()["hospital_id"] == 4
    pid = r.json()["id"]
    listed = client.get("/projects/1/pipelines", headers=U1).json()
    assert listed[0]["hospital_name"] == "SNUH" and listed[0]["steps"] == 4
    assert client.get(f"/pipelines/{pid}", headers=U2).status_code == 404
    detail = client.get(f"/pipelines/{pid}", headers=U1).json()
    step_id = detail["steps"][0]["id"]
    assert detail["statuses"][0]["key"] == "not_started"
    first = detail["steps"][0]
    assert "checklist" not in first and (first["documents"], first["drafts"], first["open_jobs"]) == (0, 0, 0)
    r = client.patch(f"/pipelines/{pid}/steps/{step_id}", headers=U1, json={"status_key": "submitted"})
    assert r.status_code == 200 and r.json()["submitted_on"] == "2026-11-21"
    r = client.patch(f"/pipelines/{pid}/steps/{step_id}", headers=U1, json={"submitted_on": None})
    assert r.json()["submitted_on"] is None
    body = {"name": "계획서", "file": {"filename": "p.xlsx", "content_base64": base64.b64encode(b"abc").decode()}}
    r = client.post(f"/pipelines/{pid}/steps/{step_id}/documents", headers=U1, json=body)
    assert r.status_code == 201 and r.json()["version_no"] == 1
    doc_id = r.json()["document"]["id"]
    assert client.get(f"/pipelines/{pid}", headers=U1).json()["steps"][0]["documents"] == 1
    page = client.get(f"/pipelines/{pid}/steps/{step_id}", headers=U1)
    assert page.status_code == 200
    page = page.json()
    assert page["step"]["id"] == step_id and page["documents"][0]["versions"][0]["version_no"] == 1
    assert page["previous_step_id"] is None and page["next_step_id"] == detail["steps"][1]["id"]
    assert (page["drafts"], page["jobs"], page["pipeline"]["hospital_name"]) == ([], [], "SNUH")
    assert client.get(f"/pipelines/{pid}/steps/{step_id}", headers=U2).status_code == 404
    assert client.get(f"/pipelines/{pid}/steps/99999", headers=U1).status_code == 404
    steps_history = client.get(f"/pipelines/{pid}/history?step_id={step_id}", headers=U1).json()
    assert steps_history and {e["step_id"] for e in steps_history} == {step_id}
    r = client.post(f"/pipelines/{pid}/steps/{step_id}/documents/{doc_id}/versions/1/download-url", headers=U1)
    assert r.status_code == 200 and r.json()["url"].startswith("https://files.example/")
    assert client.post(f"/pipelines/{pid}/steps/{step_id}/documents/{doc_id}/remove", headers=U1).status_code == 200
    assert client.post(f"/pipelines/{pid}/steps/{step_id}/documents/{doc_id}/restore", headers=U1).status_code == 200
    bad = {"name": "x", "file": {"filename": "p.xlsx", "content_base64": "%%%"}}
    r = client.post(f"/pipelines/{pid}/steps/{step_id}/documents", headers=U1, json=bad)
    assert r.status_code == 422 and "base64" in r.json()["detail"]
    r = client.post(f"/pipelines/{pid}/steps", headers=U1, json={"name": "Extra"})
    assert r.status_code == 201
    history = client.get(f"/pipelines/{pid}/history", headers=U1).json()
    assert history[0]["event_type"] == "step.added" and history[0]["mine"] is True
    assert client.delete(f"/pipelines/{pid}", headers=U1).status_code == 200
    assert client.post(f"/pipelines/{pid}/restore", headers=U1).status_code == 200
