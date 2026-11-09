import json

import pytest

from irb_watch.agent.types import TextDelta, ToolCall
from irb_watch.services.agent import (
    HISTORY_MESSAGES,
    MAX_MESSAGE_CHARS,
    MAX_MESSAGES_PER_CONVERSATION,
    MAX_STEPS,
    AgentService,
    Limits,
)
from irb_watch.services.errors import (
    AuditUnavailableError,
    ForbiddenError,
    NotFoundError,
    TooManyRequestsError,
    UnavailableError,
    ValidationError,
)
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_agent import NOW, FakeAgentData, FakeAgentModel, FakeAgentStore


def build(steps=(), *, model=True, audit=None):
    users = FakeStore()
    users.add("user-1", "u@example.com")
    users.add("user-2", "v@example.com")
    users.add("admin-1", "a@example.com", role="admin")
    users.add("gone", "g@example.com", deleted_at="2026-01-01T00:00:00+00:00")
    audit = audit or FakeAudit()
    store = FakeAgentStore(audit)
    data = FakeAgentData()
    data.add_source(1, "Public board")
    data.add_document(10, 1, "Public form")
    fake_model = FakeAgentModel(steps) if model else None
    service = AgentService(users, store, data, fake_model, audit, clock=lambda: NOW)
    return service, store, data, fake_model, audit


def ask(service, user, conv, text="hello", **kw):
    turn = service.start_turn(user, conv, text, **kw)
    return list(turn.events())


def kinds(events):
    return [e.kind for e in events]


def start(service, user="user-1"):
    return service.create_conversation(user).id


# -- the answering loop --------------------------------------------------------------


def test_an_answer_is_streamed_then_saved_with_the_question():
    service, store, *_ = build([[TextDelta("Hel"), TextDelta("lo")]])
    cid = start(service)
    events = ask(service, "user-1", cid, "  What changed?  ")
    assert kinds(events) == ["text", "text", "done"]
    assert "".join(e.data["delta"] for e in events if e.kind == "text") == "Hello"
    saved = [(m.role, m.content) for m in store.messages[cid]]
    assert saved == [("user", "What changed?"), ("assistant", "Hello")]
    assert store.conversations[cid].title == "What changed?"


def test_the_title_comes_from_the_first_message_only():
    service, store, *_ = build([[TextDelta("a")], [TextDelta("b")]])
    cid = start(service)
    ask(service, "user-1", cid, "First   question\nhere")
    ask(service, "user-1", cid, "Second")
    assert store.conversations[cid].title == "First question here"


def test_a_tool_call_is_run_and_its_result_goes_back_to_the_model():
    call = ToolCall("c1", "get_document", json.dumps({"document_id": 10}))
    service, store, data, model, _ = build([[call], [TextDelta("It is Public form.")]])
    events = ask(service, "user-1", start(service))
    assert kinds(events) == ["tool", "text", "done"]
    second_input = model.calls[1]["items"]
    output = next(i for i in second_input if i.get("type") == "function_call_output")
    assert output["call_id"] == "c1" and "Public form" in output["output"]
    assert any(i.get("type") == "function_call" for i in second_input)


def test_the_tool_runs_as_the_asking_user_not_as_whoever_the_model_names():
    call = ToolCall("c1", "list_sources", "{}")
    service, _, data, *_ = build([[call], [TextDelta("done")]])
    ask(service, "user-1", start(service))
    assert ("visible_sources", "user-1", False) in data.calls


def test_the_last_step_gets_no_tools_so_a_looping_model_stops():
    loop = ToolCall("c", "list_sources", "{}")
    service, store, _, model, _ = build([[loop]] * MAX_STEPS)
    events = ask(service, "user-1", start(service))
    assert len(model.calls) == MAX_STEPS
    assert model.calls[0]["tools"] and model.calls[-1]["tools"] == []
    assert events[-1].kind == "error" and "no answer" in events[-1].data["message"]
    assert [m.role for m in store.messages[next(iter(store.messages))]] == ["user"]


def test_a_model_failure_is_an_error_event_with_a_reason_and_saves_no_answer():
    service, store, *_ = build([RuntimeError("secret detail")])
    cid = start(service)
    events = ask(service, "user-1", cid)
    assert kinds(events) == ["error"]
    assert "RuntimeError" in events[0].data["message"]
    assert "secret detail" not in events[0].data["message"]
    assert [m.role for m in store.messages[cid]] == ["user"]


def test_an_empty_answer_is_an_error_not_a_blank_message():
    service, store, *_ = build([[]])
    events = ask(service, "user-1", start(service))
    assert events[-1].kind == "error"


def test_a_save_failure_after_streaming_is_reported():
    service, store, *_ = build([[TextDelta("hi")]])
    store.fail_add_assistant = True
    events = ask(service, "user-1", start(service))
    assert kinds(events) == ["text", "error"]
    assert "could not be saved" in events[-1].data["message"]


def test_admin_proposals_become_action_events_and_are_saved_with_the_answer():
    call = ToolCall("c1", "propose_action", json.dumps({"kind": "scan_all"}))
    service, store, *_ = build([[call], [TextDelta("Confirm below.")]])
    cid = start(service, "admin-1")
    events = ask(service, "admin-1", cid)
    assert kinds(events) == ["tool", "action", "text", "done"]
    saved = store.messages[cid][-1]
    assert saved.actions and saved.actions[0]["kind"] == "scan_all"


def test_a_plain_user_cannot_get_a_proposal_even_if_the_model_asks_for_one():
    call = ToolCall("c1", "propose_action", json.dumps({"kind": "scan_all"}))
    service, store, _, model, _ = build([[call], [TextDelta("Sorry.")]])
    events = ask(service, "user-1", start(service))
    assert "action" not in kinds(events)
    assert all(s["name"] != "propose_action" for s in model.calls[0]["tools"])


def test_the_prompt_tells_admins_about_proposals_and_users_not():
    service, _, _, model, _ = build([[TextDelta("a")], [TextDelta("b")]])
    ask(service, "user-1", start(service))
    ask(service, "admin-1", start(service, "admin-1"))
    assert "propose_action" not in model.calls[0]["instructions"]
    assert "propose_action" in model.calls[1]["instructions"]


def test_the_page_hint_is_used_only_for_a_source_or_document():
    service, _, _, model, _ = build([[TextDelta("a")]] * 3)
    cid = start(service)
    ask(service, "user-1", cid, page_kind="document", page_id=10)
    ask(service, "user-1", cid, page_kind="other", page_id=10)
    ask(service, "user-1", cid, page_kind="document", page_id=True)
    assert "document #10" in model.calls[0]["instructions"]
    assert "currently looking" not in model.calls[1]["instructions"]
    assert "currently looking" not in model.calls[2]["instructions"]


def test_only_the_newest_messages_are_sent_as_history():
    service, store, _, model, _ = build([[TextDelta("x")]])
    cid = start(service)
    for i in range(30):
        store.add_message(cid, "user-1", "user" if i % 2 == 0 else "assistant", f"m{i}", [], NOW)
    ask(service, "user-1", cid, "latest")
    items = model.calls[0]["items"]
    assert len(items) == HISTORY_MESSAGES + 1
    assert items[0]["content"] == "m10" and items[-1]["content"] == "latest"


# -- refusals, all before any streaming -----------------------------------------------


def test_a_message_must_not_be_empty_or_too_long():
    service, *_ = build()
    cid = start(service)
    with pytest.raises(ValidationError):
        service.start_turn("user-1", cid, "   ")
    with pytest.raises(ValidationError):
        service.start_turn("user-1", cid, "x" * (MAX_MESSAGE_CHARS + 1))
    service.start_turn("user-1", cid, "x" * MAX_MESSAGE_CHARS)


def test_a_full_conversation_asks_for_a_new_chat():
    service, store, *_ = build()
    cid = start(service)
    for _ in range(MAX_MESSAGES_PER_CONVERSATION):
        store.add_message(cid, "user-1", "user", "m", [], NOW)
    with pytest.raises(ValidationError, match="start a new chat"):
        service.start_turn("user-1", cid, "one more")


def test_another_users_conversation_is_not_found_for_every_action():
    service, store, *_ = build()
    cid = start(service, "user-1")
    for act in (
        lambda: service.start_turn("user-2", cid, "hi"),
        lambda: service.messages("user-2", cid),
        lambda: service.delete_conversation("user-2", cid),
    ):
        with pytest.raises(NotFoundError):
            act()
    assert cid in store.conversations
    assert service.list_conversations("user-2") == []


def test_a_deleted_account_cannot_use_the_assistant():
    service, *_ = build()
    with pytest.raises(ForbiddenError):
        service.create_conversation("gone")
    with pytest.raises(ForbiddenError):
        service.status("gone")


def test_without_a_model_the_answer_is_503_and_nothing_is_counted():
    service, store, _, _, audit = build(model=False)
    cid = start(service)
    with pytest.raises(UnavailableError):
        service.start_turn("user-1", cid, "hi")
    assert audit.entries == [] and store.messages[cid] == []


def test_the_daily_limit_is_per_account_type_and_refuses_with_429():
    service, store, *_ = build()
    cid = start(service)
    store.prior_used = 30
    with pytest.raises(TooManyRequestsError, match="30"):
        service.start_turn("user-1", cid, "hi")
    aid = start(service, "admin-1")
    service.start_turn("admin-1", aid, "hi")  # admins have 100
    store.prior_used = 100
    with pytest.raises(TooManyRequestsError):
        service.start_turn("admin-1", aid, "hi")


def test_the_limit_counts_messages_the_audit_log_recorded():
    service, store, *_ = build([[TextDelta("a")]] * 3)
    store.limits = Limits(2, 100)
    cid = start(service)
    ask(service, "user-1", cid)
    ask(service, "user-1", cid)
    with pytest.raises(TooManyRequestsError):
        service.start_turn("user-1", cid, "third")


def test_deleting_a_chat_does_not_reset_the_count():
    service, store, *_ = build([[TextDelta("a")]] * 3)
    store.limits = Limits(1, 100)
    cid = start(service)
    ask(service, "user-1", cid)
    service.delete_conversation("user-1", cid)
    with pytest.raises(TooManyRequestsError):
        service.start_turn("user-1", start(service), "again")


def test_a_limit_of_zero_switches_the_assistant_off_for_that_type():
    service, store, *_ = build()
    store.limits = Limits(0, 100)
    with pytest.raises(ForbiddenError, match="switched off"):
        service.start_turn("user-1", start(service), "hi")
    service.start_turn("admin-1", start(service, "admin-1"), "hi")


def test_if_the_audit_entry_cannot_be_written_the_model_is_not_called():
    service, store, _, model, audit = build([[TextDelta("a")]])
    cid = start(service)
    audit.fail_required = True
    with pytest.raises(AuditUnavailableError):
        service.start_turn("user-1", cid, "hi")
    assert model.calls == [] and store.messages[cid] == []


def test_the_audit_entry_holds_the_conversation_id_and_never_the_text():
    service, _, _, _, audit = build([[TextDelta("a")]])
    cid = start(service)
    ask(service, "user-1", cid, "my secret question")
    entry = next(e for e in audit.entries if e["event_type"] == "agent.message")
    assert entry["payload"] == {"conversation_id": cid}
    assert "secret" not in str(audit.entries)


# -- status, settings, retention -------------------------------------------------------


def test_status_shows_use_limit_and_why_it_is_off():
    service, store, *_ = build()
    store.prior_used = 5
    assert service.status("user-1") == {"enabled": True, "used": 5, "limit": 30, "reason": None}
    store.limits = Limits(0, 100)
    off = service.status("user-1")
    assert off["enabled"] is False and "switched off" in off["reason"]
    assert build(model=False)[0].status("user-1")["enabled"] is False


def test_only_an_admin_changes_the_limits_and_it_is_logged_first():
    service, store, _, _, audit = build()
    with pytest.raises(ForbiddenError):
        service.set_limits("user-1", 5, 5)
    new = service.set_limits("admin-1", 10, 200)
    assert (new.user_daily_limit, store.limits.admin_daily_limit) == (10, 200)
    entry = audit.entries[-1]
    assert entry["event_type"] == "agent.settings_changed"
    assert entry["payload"] == {"old": {"user": 30, "admin": 100}, "new": {"user": 10, "admin": 200}}


def test_limits_outside_0_to_1000_are_refused_and_a_failed_log_changes_nothing():
    service, store, _, _, audit = build()
    for bad in ((-1, 5), (5, 1001)):
        with pytest.raises(ValidationError):
            service.set_limits("admin-1", *bad)
    audit.fail_required = True
    with pytest.raises(AuditUnavailableError):
        service.set_limits("admin-1", 1, 1)
    assert store.limits == Limits(30, 100)


def test_old_conversations_are_purged_after_30_days_and_the_job_never_raises():
    service, store, _, _, audit = build()
    store.purge_result = 3
    assert service.purge_old() == 3
    assert (NOW - store.purged_before).days == 30
    assert audit.entries[-1]["event_type"] == "agent.purged"
    assert audit.entries[-1]["payload"] == {"conversations": 3, "days": 30}

    def boom(cutoff):
        raise RuntimeError("db down")

    store.purge_before = boom
    assert service.purge_old() == 0
