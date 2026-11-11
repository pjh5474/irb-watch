"""IRB pipeline phase 1: the admin's setup (offline, fakes only)."""

import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import get_pipeline_setup_service, get_user_store, get_verifier
from irb_watch.main import app
from irb_watch.services.errors import ConflictError, NotFoundError, ValidationError
from irb_watch.services.pipeline_admin import PipelineSetupService, StepTypeEdit
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_pipeline import FakePipelineSetupStore
from tests.test_admin_users_api import FakeVerifier


def make():
    store = FakePipelineSetupStore()
    audit = FakeAudit()
    keys = iter(f"custom_{i}" for i in range(100))
    return PipelineSetupService(store, audit, new_key=lambda: next(keys)), store, audit


# -- statuses ----------------------------------------------------------------------------------


def test_statuses_are_listed_in_order_and_new_ones_go_last():
    service, _, audit = make()
    assert [s.key for s in service.statuses()][:2] == ["not_started", "preparing"]
    added = service.add_status("admin", "  IRB   회의 대기 ", closed=False)
    assert added.label == "IRB 회의 대기" and added.key == "custom_0"
    assert service.statuses()[-1].key == "custom_0"
    assert audit.entries[-1]["event_type"] == "pipeline.status_added"
    with pytest.raises(ConflictError):
        service.add_status("admin", "승인", closed=True)
    with pytest.raises(ValidationError):
        service.add_status("admin", "   ", closed=False)


def test_rename_and_closed_flag_keep_one_open_and_one_closed():
    service, store, _ = make()
    assert service.update_status("admin", "approved", label="최종 승인").label == "최종 승인"
    service.update_status("admin", "approved", closed=False)
    service.update_status("admin", "rejected", closed=False)
    with pytest.raises(ConflictError, match="closed"):
        service.update_status("admin", "not_needed", closed=False)
    with pytest.raises(NotFoundError):
        service.update_status("admin", "nope", label="x")


def test_archive_keeps_one_of_each_kind_and_restore_checks_the_name():
    service, store, _ = make()
    service.archive_status("admin", "rejected")
    service.archive_status("admin", "not_needed")
    with pytest.raises(ConflictError, match="closed"):
        service.archive_status("admin", "approved")
    assert "rejected" not in [s.key for s in service.statuses()]
    assert "rejected" in [s.key for s in service.statuses(include_archived=True)]
    with pytest.raises(ConflictError, match="archived"):
        service.update_status("admin", "rejected", label="x")
    service.add_status("admin", "반려", closed=True)
    with pytest.raises(ConflictError, match="already exists"):
        service.restore_status("admin", "rejected")


def test_reorder_needs_every_live_key_once():
    service, *_ = make()
    keys = [s.key for s in service.statuses()]
    reordered = service.reorder_statuses("admin", list(reversed(keys)))
    assert [s.key for s in reordered] == list(reversed(keys))
    with pytest.raises(ValidationError):
        service.reorder_statuses("admin", keys[:-1])
    with pytest.raises(ValidationError):
        service.reorder_statuses("admin", keys + [keys[0]])


# -- step types --------------------------------------------------------------------------------


def edit(**changes) -> StepTypeEdit:
    base = {
        "name": "안전성 보고",
        "description": "desc",
        "instructions": "Summarise new safety information.",
        "guide_ids": [10, 10, 11],
        "form_template_id": 20,
        "input_earlier_steps": True,
        "input_board_documents": False,
        "required_documents": [" 보고서 ", "보고서", "", "CIOMS"],
    }
    return StepTypeEdit(**{**base, **changes})


def test_create_cleans_values_and_checks_guides_and_form():
    service, store, audit = make()
    created = service.create_step_type("admin", edit())
    assert created.guide_ids == (10, 11)
    assert created.required_documents == ("보고서", "CIOMS")
    assert store.updated_by[created.id] == "admin"
    assert audit.entries[-1]["event_type"] == "pipeline.step_type_created"
    with pytest.raises(ConflictError):
        service.create_step_type("admin", edit(name="초기 심의"))
    with pytest.raises(ValidationError, match="guides"):
        service.create_step_type("admin", edit(name="x", guide_ids=[99]))
    with pytest.raises(ValidationError, match="form"):
        service.create_step_type("admin", edit(name="x", form_template_id=21))
    with pytest.raises(ValidationError, match="4,000"):
        service.create_step_type("admin", edit(name="x", instructions="a" * 4001))


def test_update_replaces_values_and_logs_instruction_length():
    service, _, audit = make()
    updated = service.update_step_type("admin", 2, edit(name="변경 심의", instructions="abc"))
    assert updated.instructions == "abc" and updated.form_template_id == 20
    assert audit.entries[-1]["payload"]["instructions_chars"] == {"from": 0, "to": 3}
    with pytest.raises(ConflictError):
        service.update_step_type("admin", 2, edit(name="지속 심의"))
    with pytest.raises(NotFoundError):
        service.update_step_type("admin", 99, edit())


def test_archive_refused_while_in_a_step_order():
    service, *_ = make()
    with pytest.raises(ConflictError, match="global"):
        service.archive_step_type("admin", 1)
    archived = service.archive_step_type("admin", 4)
    assert archived.archived_at is not None
    assert 4 not in [t.id for t in service.step_types()]
    with pytest.raises(ConflictError, match="archived"):
        service.update_step_type("admin", 4, edit())
    restored = service.restore_step_type("admin", 4)
    assert restored.archived_at is None


def test_restore_refused_when_a_live_type_took_the_name():
    service, *_ = make()
    service.archive_step_type("admin", 5)
    service.create_step_type("admin", edit(name="위반/이탈 보고"))
    with pytest.raises(ConflictError, match="same name"):
        service.restore_step_type("admin", 5)


# -- default step order ------------------------------------------------------------------------


def test_save_global_and_hospital_orders():
    service, *_ = make()
    service.save_template("admin", None, [1, 3, 6])
    service.save_template("admin", 4, [1, 2, 6])
    assert service.template_for(4).step_type_ids == (1, 2, 6)
    assert service.template_for(5).step_type_ids == (1, 3, 6)  # falls back to global
    assert [t.source_id for t in service.templates()] == [None, 4]
    service.delete_template("admin", 4)
    assert service.template_for(4).step_type_ids == (1, 3, 6)
    with pytest.raises(NotFoundError):
        service.delete_template("admin", 4)


@pytest.mark.parametrize(
    ("ids", "match"),
    [([], "1 to 30"), ([1, 1], "twice"), ([1, 99], "not found"), (list(range(1, 32)), "1 to 30")],
)
def test_save_order_refuses_bad_lists(ids, match):
    service, *_ = make()
    with pytest.raises(ValidationError, match=match):
        service.save_template("admin", None, ids)


def test_save_order_refuses_archived_types_and_unknown_sources():
    service, *_ = make()
    service.archive_step_type("admin", 4)
    with pytest.raises(ValidationError):
        service.save_template("admin", None, [1, 4])
    with pytest.raises(NotFoundError):
        service.save_template("admin", 999, [1])


# -- API ---------------------------------------------------------------------------------------


@pytest.fixture
def api():
    users = FakeStore()
    users.add("admin-1", "boss@example.com", role="admin")
    users.add("user-1", "plain@example.com")
    service, store, audit = make()
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: users
    app.dependency_overrides[get_pipeline_setup_service] = lambda: service
    yield TestClient(app), store
    app.dependency_overrides.clear()


ADMIN = {"Authorization": "Bearer tok-admin-1"}


def test_api_is_admin_only(api):
    client, _ = api
    user = {"Authorization": "Bearer tok-user-1"}
    for path in ("/admin/pipeline/statuses", "/admin/pipeline/step-types", "/admin/pipeline/templates"):
        assert client.get(path, headers=user).status_code == 403
        assert client.get(path).status_code == 401


def test_api_statuses(api):
    client, _ = api
    r = client.post("/admin/pipeline/statuses", headers=ADMIN, json={"label": "보류", "closed": False})
    assert r.status_code == 201
    key = r.json()["key"]
    r = client.patch(f"/admin/pipeline/statuses/{key}", headers=ADMIN, json={"closed": True})
    assert r.status_code == 200 and r.json()["closed"] is True
    keys = [s["key"] for s in client.get("/admin/pipeline/statuses", headers=ADMIN).json()]
    r = client.put("/admin/pipeline/statuses/order", headers=ADMIN, json={"keys": keys[::-1]})
    assert r.status_code == 200 and r.json()[0]["key"] == key
    assert client.post(f"/admin/pipeline/statuses/{key}/archive", headers=ADMIN).status_code == 200
    assert client.post(f"/admin/pipeline/statuses/{key}/restore", headers=ADMIN).status_code == 200
    assert client.post("/admin/pipeline/statuses", headers=ADMIN, json={"label": ""}).status_code == 422


def test_api_step_types(api):
    client, _ = api
    body = {"name": "안전성 보고", "instructions": "x", "guide_ids": [10], "required_documents": ["CIOMS"]}
    r = client.post("/admin/pipeline/step-types", headers=ADMIN, json=body)
    assert r.status_code == 201
    created = r.json()
    assert created["guide_ids"] == [10] and created["required_documents"] == ["CIOMS"]
    r = client.put(f"/admin/pipeline/step-types/{created['id']}", headers=ADMIN, json={**body, "form_template_id": 21})
    assert r.status_code == 422 and "form" in r.json()["detail"]
    assert client.post("/admin/pipeline/step-types/1/archive", headers=ADMIN).status_code == 409
    assert client.post(f"/admin/pipeline/step-types/{created['id']}/archive", headers=ADMIN).status_code == 200
    names = [t["name"] for t in client.get("/admin/pipeline/step-types?include_archived=true", headers=ADMIN).json()]
    assert "안전성 보고" in names


def test_api_templates(api):
    client, _ = api
    r = client.put("/admin/pipeline/templates/global", headers=ADMIN, json={"step_type_ids": [1, 6]})
    assert r.status_code == 200 and r.json()["source_id"] is None
    r = client.put("/admin/pipeline/templates/sources/4", headers=ADMIN, json={"step_type_ids": [1, 2]})
    assert r.status_code == 200
    listed = client.get("/admin/pipeline/templates", headers=ADMIN).json()
    assert [t["source_id"] for t in listed] == [None, 4]
    assert client.delete("/admin/pipeline/templates/sources/4", headers=ADMIN).status_code == 204
    assert client.delete("/admin/pipeline/templates/sources/4", headers=ADMIN).status_code == 404
    assert client.put("/admin/pipeline/templates/global", headers=ADMIN, json={"step_type_ids": []}).status_code == 422
