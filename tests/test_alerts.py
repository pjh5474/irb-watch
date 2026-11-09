from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from irb_watch.mail.client import MailError
from irb_watch.services.alerts import AlertService
from irb_watch.services.runner import MANUAL, SCHEDULED, RunReport
from irb_watch.services.scan import ScanSummary
from tests.fakes import FakeAudit
from tests.fakes_mail import FakeAlertStore, FakeMailer
from tests.fakes_scan import make_source

NOW = datetime(2026, 11, 10, 6, 30, tzinfo=UTC)
GOOD = (NOW - timedelta(hours=1)).isoformat()


class Env:
    def __init__(self, sources, *, enabled=True, admins=("admin@example.com",)):
        self.sources = {s.id: s for s in sources}
        self.store = FakeAlertStore(admins)
        self.mailer = FakeMailer()
        self.audit = FakeAudit()
        self.service = AlertService(
            self.store,
            self.mailer if enabled else None,
            self.audit,
            lambda: list(self.sources.values()),
            self.sources.get,
            app_url="https://app.example.com/" if enabled else "",
            clock=lambda: NOW,
        )

    def change(self, source_id, **fields):
        self.sources[source_id] = replace(self.sources[source_id], **fields)

    def run(self, trigger=SCHEDULED, scanned=None, skipped=None, summaries=None):
        report = RunReport(trigger, scanned=scanned or {}, skipped=skipped or {})
        report.summaries.update(summaries or {})
        self.service.after_run(report)
        return report

    def kinds(self):
        return [e["payload"]["kind"] for e in self.audit.entries if e["event_type"] == "mail.sent"]


def healthy(source_id=1, **kw):
    return make_source(source_id, **{"last_scan_at": GOOD, "last_scan_status": "ok", **kw})


# ---- alarms --------------------------------------------------------------------------------


def test_a_healthy_source_sends_nothing():
    env = Env([healthy()])
    env.run(MANUAL)
    assert env.mailer.sent == [] and env.store.levels == {}


def test_a_failed_scan_sends_one_attention_mail_and_not_a_second_one():
    env = Env([healthy()])
    env.change(1, last_scan_status="failed")
    env.run(MANUAL)
    env.run(MANUAL)  # the next scan, still failing
    assert len(env.mailer.sent) == 1
    assert "확인 필요" in env.mailer.sent[0].subject
    assert env.mailer.sent[0].to == ("admin@example.com",)
    assert env.store.levels == {1: "attention"}


def test_getting_worse_is_mailed_again_but_getting_better_is_not():
    env = Env([healthy()])
    env.change(1, last_scan_status="failed")
    env.run(MANUAL)  # attention
    env.change(1, last_scan_status="needs_review")
    env.run(MANUAL)  # escalates to a problem
    assert [m.subject.split("·")[0].strip() for m in env.mailer.sent] == [
        "[IRB Watch] 확인 필요",
        "[IRB Watch] 문제",
    ]
    env.change(1, last_scan_status="failed")
    env.run(MANUAL)  # better, still not healthy: no mail, level remembered
    assert len(env.mailer.sent) == 2 and env.store.levels == {1: "attention"}
    env.change(1, last_scan_status="needs_review")
    env.run(MANUAL)  # worse again: mailed again
    assert len(env.mailer.sent) == 3


def test_recovery_mail_once_when_the_source_is_healthy_again():
    env = Env([healthy()])
    env.change(1, last_scan_status="failed")
    env.run(MANUAL)
    env.change(1, last_scan_status="ok", last_scan_at=GOOD)
    env.run(MANUAL)
    env.run(MANUAL)
    assert len(env.mailer.sent) == 2
    assert "복구됨" in env.mailer.sent[1].subject
    assert env.store.levels == {}


def test_a_source_that_goes_stale_is_caught_by_the_next_run():
    old = (NOW - timedelta(hours=40)).isoformat()
    env = Env([make_source(1, last_scan_at=old, last_scan_status="ok")])
    env.run(SCHEDULED)  # a scheduled run also sends the daily report
    assert env.kinds() == ["alarm", "daily_report"]
    assert "36" in env.mailer.sent[0].text


def test_a_running_scan_is_left_alone_until_it_finishes():
    env = Env([healthy(scan_started_at=NOW.isoformat(), last_scan_status="failed")])
    env.run(MANUAL)
    assert env.mailer.sent == []


def test_idle_and_switched_off_sources_never_alarm_and_clear_old_state():
    env = Env([make_source(1, last_scan_at=None), healthy(2, is_active=False)])
    env.store.levels = {2: "problem"}
    env.run(MANUAL)
    assert env.mailer.sent == [] and env.store.levels == {}


def test_a_paused_source_is_a_problem():
    env = Env([healthy(scans_paused_reason="login rejected")])
    env.run(MANUAL)
    assert "문제" in env.mailer.sent[0].subject


def test_a_failed_send_is_retried_next_time_and_state_is_not_saved():
    env = Env([healthy()])
    env.change(1, last_scan_status="failed")
    env.mailer.fail = "the mail service refused the message (HTTP 403)"
    env.run(MANUAL)
    assert env.store.levels == {} and env.mailer.sent == []
    failed = [e for e in env.audit.entries if e["event_type"] == "mail.failed"]
    assert len(failed) == 1 and "refused" in failed[0]["payload"]["error"]

    env.mailer.fail = None
    env.run(MANUAL)  # the problem is still there: now it goes out, once
    assert len(env.mailer.sent) == 1 and env.store.levels == {1: "attention"}


def test_the_audit_log_holds_counts_never_addresses():
    env = Env([healthy()], admins=("a@example.com", "b@example.com"))
    env.change(1, last_scan_status="failed")
    env.run(MANUAL)
    entry = next(e for e in env.audit.entries if e["event_type"] == "mail.sent")
    assert entry["payload"]["recipients"] == 2 and entry["source_id"] == 1
    assert "example.com" not in str(env.audit.entries)


def test_no_admin_address_means_no_mail_a_note_in_the_log_and_no_state():
    env = Env([healthy()], admins=())
    env.change(1, last_scan_status="failed")
    env.run(MANUAL)
    assert env.mailer.sent == [] and env.store.levels == {}
    assert any(e["event_type"] == "mail.skipped" for e in env.audit.entries)


def test_without_mail_set_up_nothing_happens_and_no_state_changes():
    env = Env([healthy()], enabled=False)
    env.change(1, last_scan_status="failed")
    env.run(SCHEDULED)
    assert env.store.levels == {} and env.audit.entries == []
    assert env.service.enabled is False


def test_one_broken_source_does_not_stop_the_others(monkeypatch):
    env = Env([healthy(1), healthy(2)])
    env.change(1, last_scan_status="failed")
    env.change(2, last_scan_status="failed")
    original = env.service._send_alarm

    def flaky(source, health):
        if source.id == 1:
            raise RuntimeError("template bug")
        return original(source, health)

    monkeypatch.setattr(env.service, "_send_alarm", flaky)
    env.run(MANUAL)
    assert env.store.levels == {2: "attention"} and len(env.mailer.sent) == 1


def test_an_unreadable_alert_state_skips_alarms_instead_of_failing():
    env = Env([healthy()])
    env.change(1, last_scan_status="failed")
    env.store.fail_reads = True
    env.run(MANUAL)  # must not raise
    assert env.mailer.sent == []


def test_evaluating_one_source_after_a_manual_scan():
    env = Env([healthy()])
    env.change(1, last_scan_status="needs_review")
    env.service.evaluate_source(1)
    env.service.evaluate_source(99)  # unknown: ignored
    assert len(env.mailer.sent) == 1


def test_members_only_sources_are_named_by_number_in_the_alarm():
    env = Env([healthy(requires_login=True, name="Secret Study")])
    env.change(1, last_scan_status="failed", last_scan_error="secret text from the page")
    env.run(MANUAL)
    mail = env.mailer.sent[0]
    assert "Secret Study" not in mail.subject + mail.text + mail.html
    assert "secret text" not in mail.text + mail.html


def test_the_link_points_at_the_admin_page_of_the_source_without_a_double_slash():
    env = Env([healthy(7)])
    env.change(7, last_scan_status="failed")
    env.run(MANUAL)
    assert "https://app.example.com/ko/admin/sources/7" in env.mailer.sent[0].text


# ---- the daily report --------------------------------------------------------------------


def summary(source_id, status="ok", **kw):
    return ScanSummary(source_id, status, **kw)


def test_the_daily_report_goes_out_once_per_day_after_the_scheduled_run():
    env = Env([healthy(1), healthy(2)])
    summaries = {1: summary(1, new=2, updated=1), 2: summary(2)}
    env.run(SCHEDULED, scanned={1: "ok", 2: "ok"}, summaries=summaries)
    env.run(SCHEDULED, scanned={1: "ok", 2: "ok"}, summaries=summaries)  # fired twice
    assert env.kinds() == ["daily_report"]
    mail = env.mailer.sent[0]
    assert "2026-11-10" in mail.subject and "새 2" in mail.subject and "수정 1" in mail.subject
    assert "claims" not in str(env.audit.entries)


def test_other_runs_send_no_daily_report():
    env = Env([healthy()])
    for trigger in (MANUAL, "retry", "catch_up"):
        env.run(trigger, scanned={1: "ok"}, summaries={1: summary(1)})
    assert env.mailer.sent == []


def test_a_failed_report_releases_its_claim_so_a_later_run_may_try_again():
    env = Env([healthy()])
    env.mailer.fail = "the mail service had a problem (HTTP 503)"
    env.run(SCHEDULED, scanned={1: "ok"}, summaries={1: summary(1)})
    assert env.store.claims == set()
    env.mailer.fail = None
    env.run(SCHEDULED, scanned={1: "ok"}, summaries={1: summary(1)})
    assert env.kinds() == ["daily_report"]


def test_the_report_lists_skipped_and_failed_sources_with_a_reason():
    env = Env([healthy(1), healthy(2, name="Guides")])
    env.run(
        SCHEDULED,
        scanned={1: "failed"},
        skipped={2: "paused after a rejected login"},
        summaries={1: summary(1, "failed", error="site unavailable")},
    )
    text = env.mailer.sent[0].text
    assert "failed" in text and "site unavailable" in text
    assert "skipped" in text and "paused after a rejected login" in text


def test_alarm_and_report_can_both_go_out_after_the_scheduled_run():
    env = Env([healthy()])
    env.change(1, last_scan_status="failed")
    env.run(SCHEDULED, scanned={1: "failed"}, summaries={1: summary(1, "failed")})
    assert sorted(env.kinds()) == ["alarm", "daily_report"]


# ---- the test mail ----------------------------------------------------------------------------


def test_send_test_goes_to_the_given_address_only():
    env = Env([healthy()])
    env.service.send_test("me@example.com")
    assert env.mailer.sent[0].to == ("me@example.com",)


def test_send_test_without_mail_set_up_is_an_error():
    env = Env([healthy()], enabled=False)
    with pytest.raises(MailError, match="not set up"):
        env.service.send_test("me@example.com")
