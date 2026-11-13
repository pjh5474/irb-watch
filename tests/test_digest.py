from datetime import UTC, datetime, timedelta

from irb_watch.mail.unsubscribe import read_token
from irb_watch.services.digest import Card, DigestService, DigestUser, SourceInfo
from tests.fakes import FakeAudit
from tests.fakes_mail import FakeDigestStore, FakeMailer

NOW = datetime(2026, 11, 10, 6, 30, tzinfo=UTC)
KEY = "k" * 64
U1 = "0b3a4c6e-1111-4222-8333-944455556666"
U2 = "1c4b5d7f-2222-4333-9444-a55566667777"
ADMIN = "2d5c6e80-3333-4444-a555-b66677778888"


def ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


def user(uid=U1, email="u@example.com", role="user", enabled=True, last=None):
    return DigestUser(uid, email, role, enabled, last)


def card(doc, source, kind="updated", hours=2, title=None):
    return Card(ago(hours), f"document.{kind}", source, doc, title or f"Title {doc}")


class Env:
    def __init__(self, users, cards, *, members=None):
        self.store = FakeDigestStore()
        self.store.user_list = users
        self.store.source_list = [
            SourceInfo(4, "Rules", False),
            SourceInfo(5, "Guides", False),
            SourceInfo(6, "Secret Study", True),
        ]
        self.store.members = members or {}
        self.store.cards = cards
        self.mailer = FakeMailer()
        self.audit = FakeAudit()
        self.service = DigestService(
            self.store, self.mailer, self.audit,
            app_url="https://app.example.com/", api_domain="api.example.com",
            unsubscribe_key=KEY, clock=lambda: NOW,
        )

    def run(self):
        return self.service.send_all()

    def to(self):
        return [m.to for m in self.mailer.sent]


def test_a_user_gets_the_new_and_updated_documents_of_public_sources_with_links():
    env = Env([user()], [card(1, 4, "new", title="Consent form"), card(2, 5, "updated", title="SOP 3")])
    assert env.run() == {
        "users": 1, "sent": 1, "failed": 0, "quiet": 0, "reminders": 0, "news_users": 1, "reminder_users": 1,
    }
    [mail] = env.mailer.sent
    assert mail.to == ("u@example.com",)
    assert "Consent form" in mail.text and "SOP 3" in mail.text
    assert "https://app.example.com/ko/documents/1" in mail.text  # no double slash
    assert "새 문서" in mail.text and "수정됨" in mail.text and "Updated" in mail.text
    assert "2 new or updated documents" in mail.subject


def test_one_mail_per_user_never_a_shared_recipient_list():
    env = Env([user(U1, "a@example.com"), user(U2, "b@example.com")], [card(1, 4)])
    env.run()
    assert sorted(env.to()) == [("a@example.com",), ("b@example.com",)]


def test_the_mail_carries_one_click_unsubscribe_headers_that_work_for_this_user_only():
    env = Env([user()], [card(1, 4)])
    env.run()
    mail = env.mailer.sent[0]
    headers = dict(mail.headers)
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    link = headers["List-Unsubscribe"].strip("<>")
    assert link.startswith("https://api.example.com/unsubscribe?token=")
    assert read_token(link.split("token=")[1], KEY) == U1
    assert "https://app.example.com/ko/unsubscribe?token=" in mail.text
    assert "https://app.example.com/ko/settings" in mail.text


def test_members_only_sources_show_a_count_only_never_a_name_or_a_title():
    env = Env(
        [user()],
        [card(1, 6, "new", title="Secret trial protocol v3"), card(2, 6, "updated", title="Secret consent")],
        members={U1: {6}},
    )
    env.run()
    mail = env.mailer.sent[0]
    everything = mail.subject + mail.text + mail.html
    assert "Secret Study" not in everything and "Secret trial" not in everything and "Secret consent" not in everything
    assert "회원 전용 출처 #6" in mail.text and "2 change(s)" in mail.text
    assert "/documents/" not in mail.text  # no link to a study document either


def test_a_user_who_is_not_a_member_sees_nothing_of_a_study():
    env = Env([user()], [card(1, 6, "new")])
    assert env.run()["quiet"] == 1 and env.mailer.sent == []


def test_an_admin_sees_every_source_but_a_study_still_as_a_count_only():
    env = Env([user(ADMIN, "admin@example.com", role="admin")], [card(1, 4, title="Public one"), card(2, 6, title="Study doc")])
    env.run()
    text = env.mailer.sent[0].text
    assert "Public one" in text and "Study doc" not in text and "#6" in text


def test_nothing_to_report_sends_nothing_and_does_not_move_the_cursor():
    env = Env([user(last=ago(30))], [card(1, 4, hours=40)])  # older than the user's last digest
    assert env.run()["quiet"] == 1
    assert env.mailer.sent == [] and env.store.cursors == {}
    assert env.audit.entries[-1]["event_type"] == "mail.digest_finished"


def test_the_first_digest_covers_the_last_24_hours_only():
    env = Env([user()], [card(1, 4, hours=20, title="Inside"), card(2, 4, hours=30, title="Too old")])
    env.run()
    text = env.mailer.sent[0].text
    assert "Inside" in text and "Too old" not in text


def test_later_digests_cover_everything_since_the_previous_one():
    env = Env([user(last=ago(60))], [card(1, 4, hours=50, title="Missed day"), card(2, 4, hours=70, title="Already sent")])
    env.run()
    text = env.mailer.sent[0].text
    assert "Missed day" in text and "Already sent" not in text


def test_the_lookback_is_capped_at_seven_days():
    env = Env([user(last=ago(24 * 30))], [card(1, 4, hours=24 * 8, title="Eight days"), card(2, 4, hours=24 * 3, title="Three days")])
    env.run()
    text = env.mailer.sent[0].text
    assert "Three days" in text and "Eight days" not in text


def test_the_cursor_moves_only_after_a_successful_send():
    env = Env([user()], [card(1, 4)])
    env.mailer.fail = "the mail service had a problem (HTTP 503)"
    assert env.run()["failed"] == 1
    assert env.store.cursors == {} and env.store.claims == set()  # claim released: tomorrow may retry
    failed = [e for e in env.audit.entries if e["event_type"] == "mail.failed"]
    assert len(failed) == 1 and "example.com" not in str(failed)
    env.mailer.fail = None
    env.run()
    assert env.store.cursors == {U1: NOW.isoformat()} and len(env.mailer.sent) == 1


def test_one_mail_per_user_per_day():
    env = Env([user()], [card(1, 4)])
    env.run()
    env.store.cursors.clear()  # even if the cursor had been lost
    env.run()
    assert len(env.mailer.sent) == 1


def test_a_switched_off_user_is_skipped_entirely():
    env = Env([user(enabled=False), user(U2, "b@example.com")], [card(1, 4)])
    env.run()
    assert env.to() == [("b@example.com",)]


def test_a_user_without_an_address_is_skipped():
    env = Env([user(email="")], [card(1, 4)])
    assert env.run()["users"] == 0


def test_one_failing_user_does_not_stop_the_others(monkeypatch):
    env = Env([user(U1, "a@example.com"), user(U2, "b@example.com")], [card(1, 4)])
    original = env.mailer.send

    def flaky(mail):
        if mail.to == ("a@example.com",):
            raise RuntimeError("template bug")
        return original(mail)

    monkeypatch.setattr(env.mailer, "send", flaky)
    counts = env.run()
    assert counts["failed"] == 1 and counts["sent"] == 1
    assert env.to() == [("b@example.com",)]


def test_a_document_with_several_events_is_listed_once_as_new_with_its_newest_title():
    env = Env([user()], [card(1, 4, "new", hours=10, title="Old title"), card(1, 4, "updated", hours=3, title="New title")])
    env.run()
    text = env.mailer.sent[0].text
    assert text.count("/ko/documents/1") == 2  # once in Korean, once in English
    assert "[새 문서] New title" in text and "Old title" not in text
    assert "1 new or updated documents" in env.mailer.sent[0].subject


def test_removed_and_review_events_are_not_part_of_the_digest():
    env = Env([user()], [card(1, 4, "removed"), card(2, 4, "needs_review")])
    assert env.run()["quiet"] == 1


def test_more_than_thirty_documents_are_listed_up_to_the_cap_and_the_rest_is_a_count():
    cards = [card(i, 4, hours=2 + i / 100, title=f"Doc {i}") for i in range(1, 41)]
    env = Env([user()], cards)
    env.run()
    mail = env.mailer.sent[0]
    assert mail.text.count("/ko/documents/") == 60  # 30 items, in both languages
    assert "10 more" in mail.text and "40 new or updated documents" in mail.subject


def test_titles_and_links_are_escaped_and_subjects_stay_one_line():
    env = Env([user()], [card(1, 4, title='<script>alert(1)</script>\r\nBcc: x@evil.example')])
    env.run()
    mail = env.mailer.sent[0]
    assert "<script>" not in mail.html and "&lt;script&gt;" in mail.html
    assert "\n" not in mail.subject and "\r" not in mail.subject


def test_the_audit_log_gets_counts_only():
    env = Env([user()], [card(1, 4)])
    env.run()
    done = env.audit.entries[-1]
    assert done["event_type"] == "mail.digest_finished" and done["payload"]["sent"] == 1
    assert "example.com" not in str(env.audit.entries)
