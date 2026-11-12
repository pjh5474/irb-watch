import base64

import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import (
    get_docgen_service,
    get_draft_job_service,
    get_user_store,
    get_verifier,
)
from irb_watch.main import app
from irb_watch.parsers.docx import parse_docx
from irb_watch.services.docgen import DocgenService
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_docgen import NOW, FakeDocgenStore, FakeWriter
from tests.fakes_jobs import finished_draft, inline_jobs
from tests.test_admin_users_api import FakeVerifier

DRAFT = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def env():
    users = FakeStore()
    users.add("user-1", "u@example.com")
    users.add("user-2", "v@example.com")
    users.add("admin-1", "a@example.com", role="admin")
    audit = FakeAudit()
    store = FakeDocgenStore(audit)
    store.add_source(1, "Public board")
    store.add_source(3, "AI-off", ai_enabled=False)
    store.add_document(10, 1)
    store.add_document(30, 3)
    store.add_guide(1, "SNUH guide", hospitals=[1])
    writer = FakeWriter(title="변경 신청서")

    def service():
        return DocgenService(users, store, writer, audit, daily_limit=5, clock=lambda: NOW)

    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: users
    app.dependency_overrides[get_docgen_service] = service
    jobs = inline_jobs()  # a job finishes inside the request
    app.dependency_overrides[get_draft_job_service] = lambda: jobs
    yield TestClient(app), store, writer, audit
    app.dependency_overrides.clear()


def auth(user):
    return {"Authorization": f"Bearer tok-{user}"}


def test_every_route_needs_a_login(env):
    client, *_ = env
    for method, path in [
        ("get", "/docgen/status"),
        ("get", "/docgen/guides"),
        ("post", "/docgen/drafts"),
        ("get", "/docgen/drafts"),
        ("get", f"/docgen/drafts/{DRAFT}"),
        ("put", f"/docgen/drafts/{DRAFT}/markdown"),
        ("post", f"/docgen/drafts/{DRAFT}/revise"),
        ("post", f"/docgen/drafts/{DRAFT}/export"),
        ("delete", f"/docgen/drafts/{DRAFT}"),
        ("get", "/admin/guides"),
        ("post", "/admin/guides"),
        ("delete", "/admin/guides/1"),
        ("get", "/admin/hospitals/1/guides"),
        ("put", "/admin/hospitals/1/guides/1"),
        ("delete", "/admin/hospitals/1/guides/1"),
    ]:
        assert getattr(client, method)(path).status_code == 401, (method, path)


def test_admin_routes_refuse_users(env):
    client, *_ = env
    assert client.get("/admin/guides", headers=auth("user-1")).status_code == 403
    assert client.put("/admin/hospitals/1/guides/1", headers=auth("user-1")).status_code == 403


def test_the_whole_flow(env):
    client, store, writer, audit = env
    h = auth("user-1")
    assert client.get("/docgen/status", headers=h).json() == {
        "enabled": True, "used": 0, "limit": 5, "reason": None
    }
    guides = client.get("/docgen/guides?source_id=1&source_id=3", headers=h).json()
    assert [g["id"] for g in guides] == [1]

    res = client.post(
        "/docgen/drafts",
        headers=h,
        json={
            "documents": [{"document_id": 10}],
            "extra_guides": [
                {"filename": "mine.txt", "content_base64": base64.b64encode(b"Add a table").decode()}
            ],
            "instruction": "visits changed",
        },
    )
    body = finished_draft(client, res, h)
    draft_id = body["draft"]["id"]
    assert body["draft"]["extra_guides"] == ["mine.txt"]
    assert body["guides"][0]["chars"] > 0
    assert body["versions"][0]["origin"] == "ai"

    edited = "# Edited by the coordinator"
    res = client.put(f"/docgen/drafts/{draft_id}/markdown", headers=h, json={"markdown": edited})
    assert res.json()["version_no"] == 2
    res = client.post(f"/docgen/drafts/{draft_id}/revise", headers=h, json={"instruction": "shorter"})
    assert res.json()["kind"] == "revision"
    assert finished_draft(client, res, h)["versions"][-1]["origin"] == "ai_revision"
    assert client.get(f"/docgen/jobs/{res.json()['id']}", headers=auth("user-2")).status_code == 404
    assert client.get("/docgen/jobs", headers=h).json() == []  # nothing open, nothing failed

    res = client.post(f"/docgen/drafts/{draft_id}/export?version_no=2", headers=h)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert "filename*=UTF-8''%EB%B3%80" in res.headers["content-disposition"]
    assert parse_docx(res.content).text == "Edited by the coordinator"

    assert [d["id"] for d in client.get("/docgen/drafts", headers=h).json()] == [draft_id]
    assert client.get(f"/docgen/drafts/{draft_id}", headers=auth("user-2")).status_code == 404
    assert client.delete(f"/docgen/drafts/{draft_id}", headers=h).status_code == 204
    assert client.get(f"/docgen/drafts/{draft_id}", headers=h).status_code == 404


def test_ai_off_source_answers_409_with_the_reason_then_works_with_the_box(env):
    client, *_ = env
    h = auth("user-1")
    res = client.post("/docgen/drafts", headers=h, json={"documents": [{"document_id": 30}]})
    assert res.status_code == 409
    assert "AI-off" in res.json()["detail"]
    res = client.post(
        "/docgen/drafts",
        headers=h,
        json={"documents": [{"document_id": 30}], "ai_override_source_ids": [3]},
    )
    assert res.status_code == 202 and res.json()["status"] == "done"
    # The refusal left no job behind.
    assert client.get("/docgen/jobs", headers=h).json() == []


def test_more_than_three_attached_guides_are_refused_before_reading(env):
    client, *_ = env
    files = [{"filename": f"g{i}.txt", "content_base64": "eA=="} for i in range(4)]
    res = client.post(
        "/docgen/drafts",
        headers=auth("user-1"),
        json={"documents": [{"document_id": 10}], "extra_guides": files},
    )
    assert res.status_code == 422


def test_a_failed_model_call_is_a_failed_job_with_a_reason(env):
    client, store, writer, audit = env
    h = auth("user-1")
    writer.fail = TimeoutError()
    res = client.post("/docgen/drafts", headers=h, json={"documents": [{"document_id": 10}]})
    assert res.status_code == 202
    job = res.json()
    assert job["status"] == "failed" and "TimeoutError" in job["error"] and job["draft_id"] is None
    assert store.drafts == {}
    # The user can read the reason afterwards, and the attempt stays counted.
    assert [j["error"] for j in client.get("/docgen/jobs", headers=h).json()] == [job["error"]]
    assert client.get("/docgen/status", headers=h).json()["used"] == 1


def test_admin_guide_library(env):
    client, store, *_ = env
    h = auth("admin-1")
    res = client.post(
        "/admin/guides",
        headers=h,
        json={"title": "Hospital B", "file": {"filename": "b.md", "content_base64": base64.b64encode("## 섹션".encode()).decode()}},
    )
    assert res.status_code == 201, res.text
    gid = res.json()["id"]
    assert res.json()["body_text"] == "## 섹션"
    assert client.put(f"/admin/hospitals/1/guides/{gid}", headers=h).status_code == 204
    ids = [g["id"] for g in client.get("/admin/hospitals/1/guides", headers=h).json()]
    assert gid in ids
    assert client.delete(f"/admin/hospitals/1/guides/{gid}", headers=h).status_code == 204
    assert client.delete(f"/admin/guides/{gid}", headers=h).status_code == 204
    listed = client.get("/admin/guides?include_archived=true", headers=h).json()
    assert next(g for g in listed if g["id"] == gid)["archived_at"] is not None
    bad = client.post(
        "/admin/guides",
        headers=h,
        json={"title": "x", "file": {"filename": "x.exe", "content_base64": "eA=="}},
    )
    assert bad.status_code == 422
