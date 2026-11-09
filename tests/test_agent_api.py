import json

import pytest
from fastapi.testclient import TestClient

from irb_watch.agent.types import TextDelta
from irb_watch.api.deps import get_agent_service, get_user_store, get_verifier
from irb_watch.main import app
from irb_watch.services.agent import AgentService, Limits
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_agent import NOW, FakeAgentData, FakeAgentModel, FakeAgentStore
from tests.test_admin_users_api import FakeVerifier


@pytest.fixture
def env():
    users = FakeStore()
    users.add("user-1", "u@example.com")
    users.add("user-2", "v@example.com")
    users.add("admin-1", "a@example.com", role="admin")
    audit = FakeAudit()
    store = FakeAgentStore(audit)
    data = FakeAgentData()
    model = FakeAgentModel([])
    holder = {"model": model}

    def service():
        return AgentService(users, store, data, holder["model"], audit, clock=lambda: NOW)

    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: users
    app.dependency_overrides[get_agent_service] = service
    yield TestClient(app), store, model, holder, audit
    app.dependency_overrides.clear()


def auth(user):
    return {"Authorization": f"Bearer tok-{user}"}


def new_chat(client, user="user-1"):
    res = client.post("/agent/conversations", headers=auth(user))
    assert res.status_code == 201
    return res.json()["id"]


def parse(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_every_route_needs_a_login(env):
    client, *_ = env
    cid = "00000000-0000-0000-0000-000000000001"
    for method, path in [
        ("get", "/agent/status"),
        ("post", "/agent/conversations"),
        ("get", "/agent/conversations"),
        ("get", f"/agent/conversations/{cid}/messages"),
        ("delete", f"/agent/conversations/{cid}"),
        ("post", f"/agent/conversations/{cid}/messages"),
        ("get", "/admin/agent/settings"),
        ("put", "/admin/agent/settings"),
    ]:
        assert getattr(client, method)(path).status_code == 401, (method, path)


def test_an_answer_streams_as_server_sent_events(env):
    client, store, model, *_ = env
    model.steps = [[TextDelta("Hel"), TextDelta("lo")]]
    cid = new_chat(client)
    res = client.post(
        f"/agent/conversations/{cid}/messages",
        headers=auth("user-1"),
        json={"content": "hi", "page": {"kind": "document", "id": 10}},
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert res.headers["cache-control"] == "no-cache"
    assert res.headers["x-accel-buffering"] == "no"
    events = parse(res.text)
    assert [k for k, _ in events] == ["text", "text", "done"]
    assert events[0][1] == {"delta": "Hel"}
    assert "document #10" in model.calls[0]["instructions"]


def test_a_failure_after_the_stream_started_is_an_error_event_with_the_reason(env):
    client, _, model, *_ = env
    model.steps = [RuntimeError("x")]
    cid = new_chat(client)
    res = client.post(
        f"/agent/conversations/{cid}/messages", headers=auth("user-1"), json={"content": "hi"}
    )
    assert res.status_code == 200
    (kind, data), = parse(res.text)
    assert kind == "error" and "RuntimeError" in data["message"]


def test_refusals_before_the_stream_are_normal_http_errors(env):
    client, store, _, holder, _ = env
    cid = new_chat(client)
    url = f"/agent/conversations/{cid}/messages"
    assert client.post(url, headers=auth("user-1"), json={"content": " "}).status_code == 422
    store.prior_used = 30
    res = client.post(url, headers=auth("user-1"), json={"content": "hi"})
    assert res.status_code == 429 and "30" in res.json()["detail"]
    store.prior_used = 0
    holder["model"] = None
    assert client.post(url, headers=auth("user-1"), json={"content": "hi"}).status_code == 503


def test_conversations_are_private_to_their_owner(env):
    client, *_ = env
    cid = new_chat(client, "user-1")
    other = auth("user-2")
    assert client.get(f"/agent/conversations/{cid}/messages", headers=other).status_code == 404
    assert client.delete(f"/agent/conversations/{cid}", headers=other).status_code == 404
    res = client.post(f"/agent/conversations/{cid}/messages", headers=other, json={"content": "hi"})
    assert res.status_code == 404
    assert client.get("/agent/conversations", headers=other).json() == []
    assert [c["id"] for c in client.get("/agent/conversations", headers=auth("user-1")).json()] == [cid]


def test_history_and_delete(env):
    client, _, model, *_ = env
    model.steps = [[TextDelta("answer")]]
    cid = new_chat(client)
    client.post(f"/agent/conversations/{cid}/messages", headers=auth("user-1"), json={"content": "q"})
    msgs = client.get(f"/agent/conversations/{cid}/messages", headers=auth("user-1")).json()
    assert [(m["role"], m["content"]) for m in msgs] == [("user", "q"), ("assistant", "answer")]
    assert client.delete(f"/agent/conversations/{cid}", headers=auth("user-1")).status_code == 204
    assert client.get("/agent/conversations", headers=auth("user-1")).json() == []


def test_a_malformed_conversation_id_is_a_422_not_a_server_error(env):
    client, *_ = env
    res = client.get("/agent/conversations/not-a-uuid/messages", headers=auth("user-1"))
    assert res.status_code == 422


def test_the_page_hint_only_accepts_known_kinds(env):
    client, *_ = env
    cid = new_chat(client)
    res = client.post(
        f"/agent/conversations/{cid}/messages",
        headers=auth("user-1"),
        json={"content": "hi", "page": {"kind": "../../etc", "id": 1}},
    )
    assert res.status_code == 422


def test_status_for_the_chat_panel(env):
    client, store, *_ = env
    store.prior_used = 4
    assert client.get("/agent/status", headers=auth("user-1")).json() == {
        "enabled": True, "used": 4, "limit": 30, "reason": None,
    }


def test_only_admins_read_or_change_the_limits(env):
    client, store, _, _, audit = env
    body = {"user_daily_limit": 12, "admin_daily_limit": 250}
    assert client.get("/admin/agent/settings", headers=auth("user-1")).status_code == 403
    assert client.put("/admin/agent/settings", headers=auth("user-1"), json=body).status_code == 403
    assert client.get("/admin/agent/settings", headers=auth("admin-1")).json() == {
        "user_daily_limit": 30, "admin_daily_limit": 100,
    }
    res = client.put("/admin/agent/settings", headers=auth("admin-1"), json=body)
    assert res.status_code == 200 and res.json() == body
    assert store.limits == Limits(12, 250)
    assert audit.entries[-1]["event_type"] == "agent.settings_changed"


def test_limits_out_of_range_are_422(env):
    client, store, *_ = env
    res = client.put(
        "/admin/agent/settings",
        headers=auth("admin-1"),
        json={"user_daily_limit": 5000, "admin_daily_limit": 1},
    )
    assert res.status_code == 422 and store.limits == Limits(30, 100)
