"""IRB pipeline phase A2: the work page and the IRB step reminders in the daily mail (offline)."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import (
    get_docgen_service,
    get_draft_job_service,
    get_pipeline_service,
    get_user_store,
    get_verifier,
)
from irb_watch.main import app
from irb_watch.services.digest import DigestService, DigestUser
from irb_watch.services.pipeline_work import is_reminder_day, reminders_for, work_lists
from irb_watch.services.pipelines import WorkStep
from tests.fakes import FakeAudit
from tests.fakes_mail import FakeDigestStore, FakeMailer
from tests.test_admin_users_api import FakeVerifier
from tests.test_pipelines import make

TODAY = date(2026, 11, 20)
KEY = "k" * 64


def ws(step_id=1, due=None, status="preparing", closed=False, submitted=None, decided=None, missing=0, **kw):
    return WorkStep(
        pipeline_id=kw.get("pipeline_id", 9), project_id=1, project_name=kw.get("project", "Study A"),
        hospital_id=4, hospital_name="SNUH", step_id=step_id, step_name=kw.get("name", f"Step {step_id}"),
        status_key=status, status_label=status, closed=closed, due_on=due, submitted_on=submitted,
        decided_on=decided, required=2 if missing else 0, required_missing=missing,
    )


def on(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


# -- rules -------------------------------------------------------------------------------------


def test_reminder_days_are_14_7_3_1_0_then_every_7_overdue():
    hits = [d for d in range(20, -30, -1) if is_reminder_day(d)]
    assert hits == [14, 7, 3, 1, 0, -7, -14, -21, -28]


def test_only_todays_reminder_days_count_and_missed_days_are_not_caught_up():
    steps = [ws(1, on(7)), ws(2, on(6)), ws(3, on(-7)), ws(4, on(15)), ws(5, on(2))]
    assert [(s.step_id, left) for s, left in reminders_for(steps, TODAY)] == [(3, -7), (1, 7)]
    # The next day: steps 1 and 3 are not repeated; step 5 now has 1 day left, step 4 has 14.
    tomorrow = TODAY + timedelta(days=1)
    assert [(s.step_id, left) for s, left in reminders_for(steps, tomorrow)] == [(5, 1), (4, 14)]


def test_submitted_closed_and_undated_steps_get_no_reminder():
    steps = [
        ws(1, on(0), status="submitted"),
        ws(2, on(0), status="approved", closed=True),
        ws(3, None),
        ws(4, on(0)),
    ]
    assert [s.step_id for s, _ in reminders_for(steps, TODAY)] == [4]


def test_a_newly_set_due_date_does_not_remind_about_days_that_already_passed():
    # Set to 2 days away today: the 7- and 3-day reminders never applied; next one at 1 day.
    assert reminders_for([ws(1, on(2))], TODAY) == []
    assert [s.step_id for s, _ in reminders_for([ws(1, on(2))], TODAY + timedelta(days=1))] == [1]


def test_work_lists():
    steps = [
        ws(1, on(-2)),  # overdue
        ws(2, on(14)),  # due in the window
        ws(3, on(15)),  # outside the window
        ws(4, on(-30), status="submitted", submitted="2026-11-01"),  # waiting: never overdue
        ws(5, None, status="submitted", submitted="2026-10-01"),
        ws(6, None, status="submitted", submitted="2026-10-02", decided="2026-11-02"),  # decided
        ws(7, on(30), missing=1),  # missing documents
        ws(8, None, status="submitted", missing=1),  # submitted: waiting, not missing
        ws(9, on(-1), status="approved", closed=True, missing=1),  # closed: nowhere
    ]
    lists = work_lists(steps, TODAY)
    assert [s.step_id for s in lists.due] == [1, 2]
    assert [s.step_id for s in lists.waiting] == [5, 4, 8]
    assert [s.step_id for s in lists.missing] == [7]


# -- the service -------------------------------------------------------------------------------


def test_work_steps_are_the_users_own_live_steps_with_names_and_checklist_counts():
    service, store, _, setup_store, *_ = make()
    setup_store.types[1] = replace(setup_store.types[1], required_documents=("Protocol", "Consent"))
    mine = service.start("u1", 1, 4)
    service.start("u2", 2, 4)
    steps = sorted(store.steps([mine.id]), key=lambda s: s.position)
    service.upload("u1", mine.id, steps[0].id, "Protocol", "p.xlsx", b"abc")
    service.remove_step("u1", mine.id, steps[3].id)
    work = service.work_steps("u1")
    assert [w.step_id for w in sorted(work, key=lambda w: w.step_id)] == [s.id for s in steps[:3]]
    first = next(w for w in work if w.step_id == steps[0].id)
    assert (first.project_name, first.hospital_name, first.required, first.required_missing) == ("Project 1", "SNUH", 2, 1)
    assert first.status_label and first.closed is False
    service.remove("u1", mine.id)
    assert service.work_steps("u1") == []


def test_reminder_steps_go_to_each_pipelines_owner_and_skip_removed_ones():
    service, store, *_ = make()
    mine = service.start("u1", 1, 4)
    theirs = service.start("u2", 2, 4)
    a, b = sorted(store.steps([mine.id]), key=lambda s: s.position)[:2]
    c = min(store.steps([theirs.id]), key=lambda s: s.position)
    service.update_step("u1", mine.id, a.id, {"due_on": "2026-11-25"})
    service.update_step("u1", mine.id, b.id, {"due_on": "2026-12-31"})  # after `until`
    service.update_step("u2", theirs.id, c.id, {"due_on": "2026-11-21"})
    found = service.reminder_steps("2026-12-01")
    assert {u: [w.step_id for w in ws_] for u, ws_ in found.items()} == {"u1": [a.id], "u2": [c.id]}
    service.remove("u2", theirs.id)
    assert set(service.reminder_steps("2026-12-01")) == {"u1"}


# -- the daily mail ----------------------------------------------------------------------------


NOW = datetime(2026, 11, 20, 0, 30, tzinfo=UTC)  # 09:30 in Seoul


class Reminders:
    def __init__(self, steps=None, fail=False):
        self.steps = steps or {}
        self.fail = fail
        self.asked = []

    def reminder_steps(self, until):
        self.asked.append(until)
        if self.fail:
            raise RuntimeError("database down")
        return self.steps


def digest(users, reminders, cards=()):
    from irb_watch.services.digest import SourceInfo

    store = FakeDigestStore()
    store.user_list = users
    store.source_list = [SourceInfo(4, "Rules", False)]
    store.cards = list(cards)
    mailer, audit = FakeMailer(), FakeAudit()
    service = DigestService(
        store, mailer, audit, app_url="https://app.example.com", api_domain="api.example.com",
        unsubscribe_key=KEY, clock=lambda: NOW, reminders=reminders,
    )
    return service, store, mailer, audit


def test_a_user_with_news_off_still_gets_reminders_and_only_on_reminder_days():
    steps = {"u1": [ws(1, on(7), name="Amend <b>2</b>", project="Study\nA"), ws(2, on(5))]}
    reminders = Reminders(steps)
    service, store, mailer, audit = digest([DigestUser("u1", "a@example.com", "user", False, None)], reminders)
    counts = service.send_all()
    assert counts["sent"] == 1 and counts["reminders"] == 1
    assert reminders.asked == ["2026-12-04"]  # today in Seoul + 14 days
    mail = mailer.sent[0]
    assert "IRB 단계 마감 알림 1건" in mail.subject and "new or updated" not in mail.subject
    assert "Amend &lt;b&gt;2&lt;/b&gt;" in mail.html and "<b>2</b>" not in mail.html  # escaped
    assert "Study A" in mail.text and "https://app.example.com/ko/pipelines/9/steps/1" in mail.text
    assert "New and updated documents" not in mail.text  # no empty news block
    assert "u1" in store.cursors
    payload = audit.entries[-1]["payload"]
    assert (payload["reminders"], payload["news_users"], payload["reminder_users"]) == (1, 0, 1)


def test_no_mail_when_no_step_is_on_a_reminder_day_or_reminders_are_off():
    service, _, mailer, _ = digest(
        [DigestUser("u1", "a@example.com", "user", False, None)], Reminders({"u1": [ws(1, on(5))]})
    )
    assert service.send_all()["quiet"] == 1 and mailer.sent == []
    off = DigestUser("u1", "a@example.com", "user", True, None, reminders_enabled=False)
    service, _, mailer, _ = digest([off], Reminders({"u1": [ws(1, on(7))]}))
    assert service.send_all()["quiet"] == 1 and mailer.sent == []
    both_off = DigestUser("u1", "a@example.com", "user", False, None, reminders_enabled=False)
    service, _, mailer, _ = digest([both_off], Reminders({"u1": [ws(1, on(7))]}))
    assert service.send_all()["users"] == 0


def test_reminders_and_news_share_one_mail():
    from irb_watch.services.digest import Card

    card = Card((NOW - timedelta(hours=2)).isoformat(), "document.new", 4, 50, "New rule")
    service, _, mailer, _ = digest(
        [DigestUser("u1", "a@example.com", "user", True, None)], Reminders({"u1": [ws(1, on(0))]}), [card]
    )
    service.send_all()
    mail = mailer.sent[0]
    assert "IRB 단계 알림 1건" in mail.subject and "새 문서·수정 1건" in mail.subject
    assert mail.text.index("IRB 단계 마감 알림") < mail.text.index("새 문서와 수정된 문서")
    assert "오늘 마감" in mail.text and "due today" in mail.text


def test_a_failed_reminder_read_still_sends_the_news_and_is_logged():
    from irb_watch.services.digest import Card

    card = Card((NOW - timedelta(hours=2)).isoformat(), "document.new", 4, 50, "New rule")
    service, _, mailer, audit = digest(
        [DigestUser("u1", "a@example.com", "user", True, None)], Reminders(fail=True), [card]
    )
    assert service.send_all()["sent"] == 1 and "New rule" in mailer.sent[0].text
    assert any(e["event_type"] == "mail.reminders_failed" for e in audit.entries)


def test_a_failed_reminder_read_and_a_failed_audit_write_still_send_the_news():
    from irb_watch.services.digest import Card

    card = Card((NOW - timedelta(hours=2)).isoformat(), "document.new", 4, 50, "New rule")
    service, _, mailer, audit = digest(
        [DigestUser("u1", "a@example.com", "user", True, None)], Reminders(fail=True), [card]
    )
    real_record = audit.record

    def record(actor, event_type, *args, **kwargs):
        if event_type == "mail.reminders_failed":
            raise RuntimeError("audit down")
        return real_record(actor, event_type, *args, **kwargs)

    audit.record = record
    assert service.send_all()["sent"] == 1 and "New rule" in mailer.sent[0].text


def test_reminder_steps_leave_closed_and_submitted_steps_out_of_the_query():
    service, store, *_ = make()
    pipeline = service.start("u1", 1, 4)
    a, b, c = sorted(store.steps([pipeline.id]), key=lambda s: s.position)[:3]
    for s, status in ((a, "approved"), (b, "submitted"), (c, None)):
        change = {"due_on": "2026-11-25", **({"status_key": status} if status else {})}
        service.update_step("u1", pipeline.id, s.id, change)
    assert [w.step_id for w in service.reminder_steps("2026-12-01")["u1"]] == [c.id]


# -- the API -----------------------------------------------------------------------------------


class NoDrafts:
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
    yield TestClient(app), service, store
    app.dependency_overrides.clear()


def test_work_api_lists_only_the_users_own_steps(api):
    client, service, store = api
    assert client.get("/work").status_code == 401
    mine = service.start("u1", 1, 4)
    theirs = service.start("u2", 2, 4)
    a, b = sorted(store.steps([mine.id]), key=lambda s: s.position)[:2]
    c = min(store.steps([theirs.id]), key=lambda s: s.position)
    today = service.today()  # the test clock, in Seoul
    service.update_step("u1", mine.id, a.id, {"due_on": (today - timedelta(days=3)).isoformat()})
    service.update_step("u1", mine.id, b.id, {"status_key": "submitted"})
    service.update_step("u2", theirs.id, c.id, {"due_on": today.isoformat()})
    body = client.get("/work", headers={"Authorization": "Bearer tok-u1"}).json()
    assert body["window_days"] == 14
    assert [s["step_id"] for s in body["due"]] == [a.id]
    assert body["today"] == today.isoformat() and body["due"][0]["days_left"] == -3
    assert body["due"][0]["project_name"] == "Project 1" and body["due"][0]["hospital_name"] == "SNUH"
    assert [s["step_id"] for s in body["waiting"]] == [b.id]
    assert body["missing"] == [] and body["drafts"] == []
