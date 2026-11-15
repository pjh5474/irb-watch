"""IRB pipeline phase C1: document lists per step type (global and per hospital) and document
slots on steps (offline)."""

import base64

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
from irb_watch.services.pipeline_admin import DocumentKindEdit as K
from tests.fakes_projects import access_service
from tests.test_admin_users_api import FakeVerifier
from tests.test_pipelines import NoDrafts, make

INITIAL, AMENDMENT = 1, 2  # seeded step types 초기 심의, 변경 심의
SNUH, OTHER = 4, 6
OTHER_WITH_NO_LIST = 5  # a hospital the fake setup store knows


def first_step(store, pipeline):
    return min(store.steps([pipeline.id]), key=lambda s: s.position)


def docs_of(service, store, pipeline, step):
    return service.step_page("u1", pipeline.id, step.id).view.documents


# -- the admin lists -----------------------------------------------------------------------------


def test_a_list_is_saved_whole_and_cleaned():
    _, _, setup, *_ = make()
    lists = setup.save_document_kinds("admin", INITIAL, None, [
        K("  신청서 ", "application", "draft", True),
        K("통지서", "received", "received", True),  # from the IRB: never required
        K("참고", "reference", "upload", True),  # reference: never required
        K("Protocol", "submission", "library", True),
    ])
    assert [(k.name, k.section, k.required) for k in lists.global_list] == [
        ("신청서", "application", True), ("통지서", "received", False),
        ("참고", "reference", False), ("Protocol", "submission", True),
    ]
    with pytest.raises(ValidationError, match="twice"):
        setup.save_document_kinds("admin", INITIAL, None, [K("A"), K("a")])
    with pytest.raises(ValidationError, match="section"):
        setup.save_document_kinds("admin", INITIAL, None, [K("A", "elsewhere")])
    with pytest.raises(ValidationError, match="origin"):
        setup.save_document_kinds("admin", INITIAL, None, [K("A", origin="magic")])
    with pytest.raises(ValidationError, match="at most 50"):
        setup.save_document_kinds("admin", INITIAL, None, [K(f"D{i}") for i in range(51)])
    with pytest.raises(NotFoundError):
        setup.save_document_kinds("admin", 999, None, [K("A")])


def test_saving_keeps_rows_by_id_moves_names_and_archives_the_rest():
    _, _, setup, setup_store, *_ = make()
    first = setup.save_document_kinds("admin", INITIAL, None, [K("A"), K("B")]).global_list
    a, b = first
    # Swap the names, drop nothing yet: ids stay with their rows.
    swapped = setup.save_document_kinds("admin", INITIAL, None, [K("B", id=a.id), K("A", id=b.id)]).global_list
    assert [(k.id, k.name) for k in swapped] == [(a.id, "B"), (b.id, "A")]
    kept = setup.save_document_kinds("admin", INITIAL, None, [K("A", id=b.id)]).global_list
    assert [k.id for k in kept] == [b.id]
    assert setup_store.kind_rows[a.id].archived_at is not None  # archived, not deleted (slots point at it)


def test_a_hospital_list_is_its_own_and_can_go_back_to_global():
    _, _, setup, *_ = make()
    setup.save_document_kinds("admin", INITIAL, None, [K("Global doc")])
    own = setup.save_document_kinds("admin", INITIAL, SNUH, [K("SNUH doc")])
    assert [k.name for k in own.hospitals[SNUH]] == ["SNUH doc"]
    assert [k.name for k in setup.document_kinds_for([INITIAL], SNUH)[INITIAL]] == ["SNUH doc"]
    assert [k.name for k in setup.document_kinds_for([INITIAL], OTHER)[INITIAL]] == ["Global doc"]
    # A hospital list needs a document (owner: refused, not an empty list that changes nothing).
    with pytest.raises(ValidationError, match="at least one document"):
        setup.save_document_kinds("admin", INITIAL, SNUH, [])
    with pytest.raises(ValidationError, match="at least one document"):
        setup.save_document_kinds("admin", INITIAL, OTHER_WITH_NO_LIST, [])
    assert setup.save_document_kinds("admin", AMENDMENT, None, []).global_list == []  # the global list may be empty
    # A row id of another list is refused.
    global_id = setup.document_lists(INITIAL).global_list[0].id
    with pytest.raises(ValidationError, match="another list"):
        setup.save_document_kinds("admin", INITIAL, SNUH, [K("x", id=global_id)])
    back = setup.delete_document_kinds("admin", INITIAL, SNUH)
    assert SNUH not in back.hospitals
    assert [k.name for k in setup.document_kinds_for([INITIAL], SNUH)[INITIAL]] == ["Global doc"]
    with pytest.raises(NotFoundError):
        setup.delete_document_kinds("admin", INITIAL, SNUH)
    with pytest.raises(NotFoundError, match="hospital"):
        setup.save_document_kinds("admin", INITIAL, 99, [K("x")])


# -- slots on new steps ----------------------------------------------------------------------------


def test_new_steps_copy_the_hospitals_list_or_the_global_one_and_keep_it():
    service, store, setup, *_ = make()
    setup.save_document_kinds("admin", INITIAL, None, [K("신청서", "application", "draft", True), K("Protocol", required=True)])
    setup.save_document_kinds("admin", AMENDMENT, SNUH, [K("변경대비표", "submission", "draft", True)])
    pipeline = service.start("u1", 1, SNUH)
    steps = sorted(store.steps([pipeline.id]), key=lambda s: s.position)
    by_step = {s.step_type_id: [d for d in store.documents([s.id])] for s in steps}
    assert [(d.name, d.section, d.origin, d.required, d.status) for d in by_step[INITIAL]] == [
        ("신청서", "application", "draft", True, "needed"), ("Protocol", "submission", "upload", True, "needed"),
    ]
    assert [d.name for d in by_step[AMENDMENT]] == ["변경대비표"]  # SNUH's own list
    assert all(d.kind_id is not None for d in by_step[INITIAL])
    # Later list edits do not change existing steps; a step added later gets the new list.
    setup.save_document_kinds("admin", INITIAL, None, [K("New only")])
    assert [d.name for d in store.documents([steps[0].id])] == ["신청서", "Protocol"]
    added = service.add_step("u1", pipeline.id, None, INITIAL)
    assert [d.name for d in store.documents([added.id])] == ["New only"]
    free = service.add_step("u1", pipeline.id, "Free step", None)
    assert store.documents([free.id]) == []


# -- slot actions ------------------------------------------------------------------------------------


@pytest.fixture
def slots():
    service, store, setup, *_ = make()
    setup.save_document_kinds("admin", INITIAL, None, [K("Protocol", required=True), K("통지서", "received", "received")])
    pipeline = service.start("u1", 1, SNUH)
    step = first_step(store, pipeline)
    return service, store, pipeline, step


def test_upload_into_a_slot_sets_it_in_progress_and_final_needs_a_file(slots):
    service, store, pipeline, step = slots
    protocol = next(d for d in docs_of(service, store, pipeline, step) if d.name == "Protocol")
    with pytest.raises(ValidationError, match="upload a file"):
        service.update_document("u1", pipeline.id, step.id, protocol.id, {"status": "final"})
    doc, version = service.upload("u1", pipeline.id, step.id, "", "p.xlsx", b"abc", document_id=protocol.id)
    assert (doc.id, doc.status, version.version_no) == (protocol.id, "in_progress", 1)
    final = service.update_document("u1", pipeline.id, step.id, protocol.id, {"status": "final"})
    assert final.status == "final" and final.done and final.versions
    # A newer file makes it in progress again (the user marks the newest final).
    doc, _ = service.upload("u1", pipeline.id, step.id, "", "p2.xlsx", b"abcd", document_id=protocol.id)
    assert doc.status == "in_progress"
    events = [e for e in store.event_rows if e.event_type == "document.uploaded"]
    assert events[-1].payload["status"] == {"from": "final", "to": "in_progress"}
    # Reopening picks the status: in progress with a file, needed without one.
    service.update_document("u1", pipeline.id, step.id, protocol.id, {"status": "not_needed"})
    assert service.update_document("u1", pipeline.id, step.id, protocol.id, {"status": "needed"}).status == "in_progress"
    with pytest.raises(ValidationError, match="status"):
        service.update_document("u1", pipeline.id, step.id, protocol.id, {"status": "lost"})


def test_users_add_their_own_slots_and_only_those_can_be_renamed(slots):
    service, store, pipeline, step = slots
    own = service.add_document("u1", pipeline.id, step.id, "심의 서류 바인더", "reference")
    assert (own.section, own.required, own.status, own.kind_id) == ("reference", False, "needed", None)
    with pytest.raises(ConflictError, match="exists"):
        service.add_document("u1", pipeline.id, step.id, "protocol")  # any case
    with pytest.raises(ValidationError, match="section"):
        service.add_document("u1", pipeline.id, step.id, "X", "nowhere")
    renamed = service.update_document("u1", pipeline.id, step.id, own.id, {"name": "바인더", "section": "submission", "note": "paper copy"})
    assert (renamed.name, renamed.section, renamed.note) == ("바인더", "submission", "paper copy")
    event = [e for e in store.event_rows if e.event_type == "document.updated"][-1]
    assert event.payload["note"] is True and "paper copy" not in str(event.payload)  # the text stays out
    listed = next(d for d in store.documents([step.id]) if d.name == "Protocol")
    with pytest.raises(ValidationError, match="listed document"):
        service.update_document("u1", pipeline.id, step.id, listed.id, {"name": "Protocol v2"})
    # A listed slot is never removed (owner): the user marks it not needed instead.
    with pytest.raises(ValidationError, match="not needed instead"):
        service.remove_document("u1", pipeline.id, step.id, listed.id)
    assert service.remove_document("u1", pipeline.id, step.id, renamed.id).deleted_at is not None
    # A user's slot is never required, so the checklist still has only the listed one.
    assert [c.name for c in service.step_page("u1", pipeline.id, step.id).view.checklist] == ["Protocol"]


def test_a_listed_slot_stays_locked_after_the_hospital_list_is_removed():
    service, store, setup, *_ = make()
    setup.save_document_kinds("admin", INITIAL, SNUH, [K("SNUH form", required=True)])
    pipeline = service.start("u1", 1, SNUH)
    step = first_step(store, pipeline)
    slot = store.documents([step.id])[0]
    setup.delete_document_kinds("admin", INITIAL, SNUH)
    store.docs[slot.id] = store.docs[slot.id].__class__(**{**store.docs[slot.id].__dict__, "kind_id": None})  # on delete set null
    with pytest.raises(ValidationError, match="listed document"):
        service.update_document("u1", pipeline.id, step.id, slot.id, {"name": "Renamed"})
    with pytest.raises(ValidationError, match="not needed instead"):
        service.remove_document("u1", pipeline.id, step.id, slot.id)


def test_restore_keeps_the_document_limit(slots, monkeypatch):
    service, store, pipeline, step = slots
    own = service.add_document("u1", pipeline.id, step.id, "Mine")
    service.remove_document("u1", pipeline.id, step.id, own.id)
    monkeypatch.setattr("irb_watch.services.pipelines.MAX_DOCUMENTS", 2)  # Protocol and 통지서 are live
    with pytest.raises(ConflictError, match="at most 2 documents"):
        service.restore_document("u1", pipeline.id, step.id, own.id)


def test_an_upload_into_a_slot_ignores_the_section(slots):
    service, store, pipeline, step = slots
    protocol = next(d for d in store.documents([step.id]) if d.name == "Protocol")
    doc, version = service.upload("u1", pipeline.id, step.id, "", "p.xlsx", b"abc", document_id=protocol.id, section="old-value")
    assert (doc.id, doc.status, version.version_no, len(doc.versions)) == (protocol.id, "in_progress", 1, 1)
    with pytest.raises(NotFoundError):
        service.upload("u1", pipeline.id, step.id, "", "p.xlsx", b"abcd", document_id=99999)


def test_upload_by_name_makes_a_users_slot_in_the_section_asked(slots):
    service, store, pipeline, step = slots
    doc, _ = service.upload("u1", pipeline.id, step.id, "접수증", "r.xlsx", b"abc", section="received")
    assert (doc.section, doc.status, doc.kind_id) == ("received", "in_progress", None)
    same, version = service.upload("u1", pipeline.id, step.id, "접수증", "r2.xlsx", b"abcd")
    assert same.id == doc.id and version.version_no == 2
    with pytest.raises(ValidationError, match="section"):
        service.upload("u1", pipeline.id, step.id, "Y", "y.xlsx", b"abc", section="nowhere")


def test_slots_are_shown_by_section_and_can_be_reordered(slots):
    service, store, pipeline, step = slots
    service.add_document("u1", pipeline.id, step.id, "신청서", "application")
    service.add_document("u1", pipeline.id, step.id, "CRF")
    names = [d.name for d in docs_of(service, store, pipeline, step)]
    assert names == ["신청서", "통지서", "Protocol", "CRF"]  # 신청서 및 통지서, then 제출서류
    ids = {d.name: d.id for d in docs_of(service, store, pipeline, step)}
    order = [ids["신청서"], ids["통지서"], ids["CRF"], ids["Protocol"]]
    reordered = service.reorder_documents("u1", pipeline.id, step.id, order)
    assert [d.name for d in reordered] == ["신청서", "통지서", "CRF", "Protocol"]
    with pytest.raises(ConflictError, match="reload"):
        service.reorder_documents("u1", pipeline.id, step.id, order[:2])


def test_a_reader_cannot_change_slots(slots):
    service, store, pipeline, step = slots
    with pytest.raises(NotFoundError):
        service.add_document("u2", pipeline.id, step.id, "X")


# -- API -----------------------------------------------------------------------------------------------


@pytest.fixture
def api():
    service, store, setup, *_ = make()
    service._users.add("admin-1", "boss@example.com", role="admin")
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: service._users
    app.dependency_overrides[get_pipeline_service] = lambda: service
    app.dependency_overrides[get_pipeline_setup_service] = lambda: setup
    app.dependency_overrides[get_docgen_service] = NoDrafts
    app.dependency_overrides[get_draft_job_service] = NoDrafts
    access = access_service(service._users, store)[0]
    app.dependency_overrides[get_project_access_service] = lambda: access
    yield TestClient(app), store
    app.dependency_overrides.clear()


U1 = {"Authorization": "Bearer tok-u1"}
ADMIN = {"Authorization": "Bearer tok-admin-1"}


def test_api_lists_and_slots(api):
    client, store = api
    rows = [
        {"name": "신청서", "section": "application", "origin": "draft", "required": True},
        {"name": "Protocol", "section": "submission", "origin": "library", "required": True},
    ]
    assert client.put(f"/admin/pipeline/step-types/{INITIAL}/documents/global", headers=U1, json={"documents": rows}).status_code == 403
    r = client.put(f"/admin/pipeline/step-types/{INITIAL}/documents/global", headers=ADMIN, json={"documents": rows})
    assert r.status_code == 200 and [d["name"] for d in r.json()["global_documents"]] == ["신청서", "Protocol"]
    r = client.put(f"/admin/pipeline/step-types/{INITIAL}/documents/hospitals/5", headers=ADMIN, json={"documents": rows[:1]})
    assert [(h["hospital_id"], [d["name"] for d in h["documents"]]) for h in r.json()["hospitals"]] == [(5, ["신청서"])]
    r = client.put(f"/admin/pipeline/step-types/{INITIAL}/documents/global", headers=ADMIN, json={"documents": [rows[0], rows[0]]})
    assert r.status_code == 422 and "twice" in r.json()["detail"]
    assert client.delete(f"/admin/pipeline/step-types/{INITIAL}/documents/hospitals/5", headers=ADMIN).json()["hospitals"] == []
    assert client.get(f"/admin/pipeline/step-types/{INITIAL}/documents", headers=ADMIN).status_code == 200

    pid = client.post("/projects/1/pipelines", headers=U1, json={"hospital_id": SNUH}).json()["id"]
    detail = client.get(f"/pipelines/{pid}", headers=U1).json()
    step_id = detail["steps"][0]["id"]
    assert (detail["steps"][0]["required"], detail["steps"][0]["required_missing"]) == (2, 2)
    page = client.get(f"/pipelines/{pid}/steps/{step_id}", headers=U1).json()
    docs = {d["name"]: d for d in page["documents"]}
    assert docs["Protocol"]["listed"] is True and docs["Protocol"]["status"] == "needed"
    assert [(c["name"], c["done"]) for c in page["checklist"]] == [("신청서", False), ("Protocol", False)]
    body = {"document_id": docs["Protocol"]["id"], "file": {"filename": "p.xlsx", "content_base64": base64.b64encode(b"abc").decode()}}
    r = client.post(f"/pipelines/{pid}/steps/{step_id}/documents", headers=U1, json=body)
    assert r.status_code == 201 and r.json()["document"]["status"] == "in_progress"
    url = f"/pipelines/{pid}/steps/{step_id}/documents/{docs['Protocol']['id']}"
    assert client.patch(url, headers=U1, json={"status": "final"}).json()["status"] == "final"
    assert client.patch(url, headers=U1, json={"status": None}).status_code == 422
    r = client.post(f"/pipelines/{pid}/steps/{step_id}/document-slots", headers=U1, json={"name": "바인더", "section": "reference"})
    assert r.status_code == 201 and r.json()["listed"] is False
    ids = [d["id"] for d in client.get(f"/pipelines/{pid}/steps/{step_id}", headers=U1).json()["documents"]]
    r = client.put(f"/pipelines/{pid}/steps/{step_id}/documents/order", headers=U1, json={"document_ids": list(reversed(ids))})
    assert r.status_code == 200
    assert client.get(f"/pipelines/{pid}", headers=U1).json()["steps"][0]["required_missing"] == 1
