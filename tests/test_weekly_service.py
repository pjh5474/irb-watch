from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import get_audit_sink, get_user_store, get_verifier, get_weekly_service
from irb_watch.core.weekly import Event, UserInfo
from irb_watch.mail.client import MailError
from irb_watch.main import app
from irb_watch.scheduler import ScanScheduler, next_daily_run
from irb_watch.services.weekly import WeeklyStatsService
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_mail import FakeMailer, FakeWeeklyStore
from tests.fakes_scan import make_source
from tests.test_admin_users_api import FakeVerifier

NOW = datetime(2026, 11, 9, 7, 0, tzinfo=UTC)  # a Monday, 16:00 KST here (the test clock is UTC)


def at(days_ago):
    return NOW - timedelta(days=days_ago)


class Env:
    def __init__(self, *, enabled=True, admins=("admin@example.com",), sources=None):
        self.store = FakeWeeklyStore(admins)
        self.mailer = FakeMailer()
        self.audit = FakeAudit()
        self.sources = sources or [
            make_source(4, name="Rules", last_scan_at=at(0.1).isoformat(), last_scan_status="ok"),
            make_source(5, name="Guides", last_scan_at=at(0.1).isoformat(), last_scan_status="failed"),
            make_source(6, name="Secret Study", requires_login=True, last_scan_at=at(0.1).isoformat(), last_scan_status="needs_review"),
        ]
        self.service = WeeklyStatsService(
            self.store,
            self.mailer if enabled else None,
            self.audit,
            lambda: list(self.sources),
            app_url="https://app.example.com/" if enabled else "",
            clock=lambda: NOW,
        )
        self.store.user_list = [
            UserInfo("u1", "heavy@example.com", at(40), at(1)),
            UserInfo("u2", "new@example.com", at(2), at(2)),
            UserInfo("u3", "quiet@example.com", at(40), at(30)),
        ]
        self.store.event_list = [
            Event("summary.requested", "u1", 4, at(1)),
            Event("summary.regenerated", "u1", 4, at(2)),
            Event("summary.requested", "u1", 4, at(9)),
            Event("file.download_link", "u1", 4, at(1)),
            Event("file.download_link", "u1", 6, at(1)),
            Event("document.new", None, 4, at(1)),
            Event("document.updated", None, 6, at(1)),
            Event("scan.run_finished", None, None, at(1), {"scanned": 3, "failed": 1, "skipped": 0}),
        ]


def kinds(env):
    return [e["payload"]["kind"] for e in env.audit.entries if e["event_type"] == "mail.sent"]


def test_the_weekly_mail_goes_to_all_admins_with_the_figures():
    env = Env(admins=("a@example.com", "b@example.com"))
    assert env.service.send_scheduled() is True
    [mail] = env.mailer.sent
    assert mail.to == ("a@example.com", "b@example.com")
    assert "요약 2" in mail.subject and "다운로드 2" in mail.subject and "로그인 2" in mail.subject
    text = mail.text
    assert "AI summaries requested: 2 (+1)" in text  # 1 the week before
    assert "File downloads: 2 (+2)" in text
    assert "Accounts that signed in: 2" in text and "New accounts: 1" in text
    assert "heavy@example.com" in text and "new@example.com" in text
    assert "quiet@example.com" not in text  # no activity this week
    assert "3 daily runs" not in text and "1 daily runs, 3 source scans, 1 failed" in text
    assert "https://app.example.com/ko/admin/sources" in text


def test_the_period_is_the_last_seven_days():
    env = Env()
    env.service.send_scheduled()
    assert "2026-11-02 ~ 2026-11-09" in env.mailer.sent[0].text


def test_a_study_appears_as_a_number_and_sources_needing_attention_are_listed():
    env = Env()
    env.service.send_scheduled()
    mail = env.mailer.sent[0]
    everything = mail.subject + mail.text + mail.html
    assert "Secret Study" not in everything
    assert "회원 전용 출처 #6" in mail.text and "Members-only source #6" in mail.text
    assert "Guides: Needs attention" in mail.text and "Members-only source #6: Problem" in mail.text
    assert "Rules: 1 / 0" in mail.text  # one new document, none updated


def test_once_per_week_even_if_the_job_fires_twice():
    env = Env()
    assert env.service.send_scheduled() is True
    assert env.service.send_scheduled() is False
    assert len(env.mailer.sent) == 1 and kinds(env) == ["weekly_stats"]
    env.service._clock = lambda: NOW + timedelta(days=7)  # next week: allowed again
    assert env.service.send_scheduled() is True


def test_a_failed_send_releases_the_claim_so_a_later_run_may_try_again():
    env = Env()
    env.mailer.fail = "the mail service had a problem (HTTP 503)"
    assert env.service.send_scheduled() is False
    assert env.store.claims == set()
    assert any(e["event_type"] == "mail.failed" and e["payload"]["kind"] == "weekly_stats" for e in env.audit.entries)
    env.mailer.fail = None
    assert env.service.send_scheduled() is True


def test_a_crash_while_reading_the_data_never_raises_and_releases_the_claim():
    env = Env()
    env.store.fail_events = True
    assert env.service.send_scheduled() is False  # must not raise into the scheduler
    assert env.store.claims == set() and env.mailer.sent == []


def test_no_admin_address_means_no_mail_and_a_note_in_the_log():
    env = Env(admins=())
    assert env.service.send_scheduled() is False
    assert any(e["event_type"] == "mail.skipped" for e in env.audit.entries)
    assert env.store.claims == set()


def test_without_mail_set_up_nothing_happens():
    env = Env(enabled=False)
    assert env.service.enabled is False and env.service.send_scheduled() is False
    assert env.store.claims == set() and env.audit.entries == []


def test_the_audit_log_holds_counts_never_addresses():
    env = Env(admins=("a@example.com", "b@example.com"))
    env.service.send_scheduled()
    entry = next(e for e in env.audit.entries if e["event_type"] == "mail.sent")
    assert entry["payload"]["recipients"] == 2 and entry["payload"]["week"] == "weekly:2026-W46"
    assert "example.com" not in str(env.audit.entries)


def test_emails_and_names_are_escaped_in_the_html():
    env = Env()
    env.store.user_list = [UserInfo("u1", "<script>x</script>@example.com", at(40), at(1))]
    env.service.send_scheduled()
    html = env.mailer.sent[0].html
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_more_than_fifty_active_accounts_are_cut_with_a_count():
    env = Env()
    env.store.user_list = [UserInfo(f"u{i}", f"u{i}@example.com", at(40), at(1)) for i in range(60)]
    env.service.send_scheduled()
    assert "and 10 more" in env.mailer.sent[0].text and env.mailer.sent[0].text.count("@example.com |") == 100  # 50 rows, 2 languages


def test_a_quiet_week_still_sends_a_mail_of_zeros():
    env = Env()
    env.store.event_list, env.store.user_list = [], []
    env.service.send_scheduled()
    text = env.mailer.sent[0].text
    assert "AI summaries requested: 0 (0)" in text and "none" in text


# ---- the preview --------------------------------------------------------------------------------


def test_the_preview_goes_to_one_address_only_and_takes_no_claim():
    env = Env(admins=("a@example.com", "b@example.com"))
    env.service.preview("me@example.com")
    assert env.mailer.sent[0].to == ("me@example.com",)
    assert "Preview" in env.mailer.sent[0].text
    assert env.store.claims == set()
    assert env.service.send_scheduled() is True  # Monday's real mail is not used up


def test_the_preview_without_mail_set_up_is_an_error():
    env = Env(enabled=False)
    with pytest.raises(MailError, match="not set up"):
        env.service.preview("me@example.com")


# ---- the scheduler job ---------------------------------------------------------------------------


class StubRunner:
    def run_all(self, trigger):
        pass

    def run_retry(self, source_id):
        pass


def test_the_weekly_job_is_registered_only_when_given():
    with_weekly = ScanScheduler(StubRunner(), "0 6 * * *", "Asia/Seoul", weekly=lambda: None, weekly_cron="0 7 * * mon")
    with_weekly.start()
    try:
        assert "weekly-stats" in with_weekly.job_ids()
    finally:
        with_weekly.stop()
    without = ScanScheduler(StubRunner(), "0 6 * * *", "Asia/Seoul")
    without.start()
    try:
        assert "weekly-stats" not in without.job_ids()
    finally:
        without.stop()


def test_the_default_weekly_time_is_monday_0700_in_seoul():
    from irb_watch.config import Settings

    wednesday = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
    fire = next_daily_run(Settings().weekly_stats_cron, "Asia/Seoul", wednesday)
    assert fire is not None and (fire.isoweekday(), fire.hour, fire.minute) == (1, 7, 0)
    assert fire.date().isoformat() == "2026-11-16"  # the coming Monday


def test_a_numeric_weekday_would_be_off_by_one_which_is_why_names_are_used():
    wednesday = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
    numeric = next_daily_run("0 7 * * 1", "Asia/Seoul", wednesday)
    assert numeric is not None and numeric.isoweekday() == 2  # Tuesday, not Monday


# ---- the preview over HTTP ----------------------------------------------------------------------


@pytest.fixture
def api():
    users = FakeStore()
    users.add("admin-1", "admin@example.com", role="admin")
    users.add("user-1", "u@example.com")
    users.add("no-mail", None, role="admin")
    env = Env(admins=("other-admin@example.com",))
    state = {"service": env.service}
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: users
    app.dependency_overrides[get_audit_sink] = lambda: env.audit
    app.dependency_overrides[get_weekly_service] = lambda: state["service"]
    yield TestClient(app), env, state
    app.dependency_overrides.clear()


def auth(user):
    return {"Authorization": f"Bearer tok-{user}"}


def test_the_preview_endpoint_mails_the_caller_only(api):
    client, env, _ = api
    res = client.post("/admin/mail/weekly-preview", headers=auth("admin-1"))
    assert res.status_code == 200 and res.json() == {"status": "sent"}
    assert env.mailer.sent[0].to == ("admin@example.com",)  # not the other admin
    assert env.audit.entries[-1]["event_type"] == "mail.weekly_preview_sent"


def test_only_admins_may_ask_for_a_preview(api):
    client, env, _ = api
    assert client.post("/admin/mail/weekly-preview").status_code == 401
    assert client.post("/admin/mail/weekly-preview", headers=auth("user-1")).status_code == 403
    assert env.mailer.sent == []


def test_the_preview_endpoint_reports_missing_setup_and_provider_refusals(api):
    client, env, state = api
    env.mailer.fail = "the mail service refused the message (HTTP 403: domain is not verified)"
    res = client.post("/admin/mail/weekly-preview", headers=auth("admin-1"))
    assert res.status_code == 502 and "domain is not verified" in res.json()["detail"]
    state["service"] = Env(enabled=False).service
    res = client.post("/admin/mail/weekly-preview", headers=auth("admin-1"))
    assert res.status_code == 409 and "RESEND_API_KEY" in res.json()["detail"]
    assert client.post("/admin/mail/weekly-preview", headers=auth("no-mail")).status_code in (409, 422)
