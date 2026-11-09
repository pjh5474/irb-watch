from irb_watch.core import health as h
from irb_watch.mail import templates as t

TO = ("admin@example.com",)
URL = "https://app.example.com/ko/admin/sources/5"


def info(**kw):
    base = {
        "source_id": 5,
        "name": "SNUH rules",
        "members_only": False,
        "level": h.PROBLEM,
        "reason": h.PAUSED,
        "error": "login rejected: wrong password",
        "stale_hours": 36,
        "admin_url": URL,
    }
    return t.AlertInfo(**{**base, **kw})


def test_alarm_is_korean_first_with_english_below_and_a_link():
    mail = t.alarm_mail(TO, info())
    assert mail.to == TO
    assert "SNUH rules" in mail.subject and "문제" in mail.subject and "Problem" in mail.subject
    ko, en = mail.text.split("---")
    assert "로그인이 거부되어" in ko and "Scans are paused after a rejected login" in en
    assert URL in ko and URL in en
    assert f'href="{URL}"' in mail.html


def test_attention_wording_and_the_stale_hours():
    mail = t.alarm_mail(TO, info(level=h.ATTENTION, reason=h.STALE, error=None, stale_hours=48))
    assert "확인 필요" in mail.subject and "Needs attention" in mail.subject
    assert "48시간" in mail.text and "48 hours" in mail.text


def test_the_scan_error_text_comes_from_the_web_and_is_escaped_and_cut():
    nasty = '<script>alert(1)</script>' + "x" * 500
    mail = t.alarm_mail(TO, info(error=nasty))
    assert "<script>" not in mail.html
    assert "&lt;script&gt;" in mail.html
    assert "x" * (t.MAX_ERROR_CHARS + 1) not in mail.text


def test_a_members_only_source_is_named_by_number_and_its_error_text_is_left_out():
    mail = t.alarm_mail(TO, info(name="Secret Drug Trial 7", members_only=True))
    for part in (mail.subject, mail.text, mail.html):
        assert "Secret Drug Trial 7" not in part
        assert "wrong password" not in part
    assert "회원 전용 출처 #5" in mail.text and "Members-only source #5" in mail.text


def test_subjects_are_one_clean_line_and_capped():
    mail = t.alarm_mail(TO, info(name="Line1\r\nBcc: evil@example.com\x00" + "y" * 400))
    assert "\n" not in mail.subject and "\r" not in mail.subject and "\x00" not in mail.subject
    assert len(mail.subject) <= 140


def test_recovery_mail():
    mail = t.recovery_mail(TO, 5, "SNUH rules", False, URL)
    assert "복구됨" in mail.subject and "Recovered" in mail.subject
    assert "SNUH rules" in mail.subject and "healthy again" in mail.text
    hidden = t.recovery_mail(TO, 6, "Secret Study", True, URL)
    assert "Secret Study" not in hidden.subject + hidden.text + hidden.html


def test_daily_report_has_counts_problems_first_in_the_subject_and_hides_study_names():
    lines = [
        t.ReportLine(4, "Rules", False, "ok", new=2, updated=1, removed=0),
        t.ReportLine(5, "Guides", False, "failed", note="site unavailable <b>x</b>"),
        t.ReportLine(6, "Secret Study", True, "ok", new=3),
    ]
    mail = t.report_mail(TO, "2026-11-10", lines, "https://app.example.com/ko/admin/sources")
    assert "2026-11-10" in mail.subject and "새 5" in mail.subject and "수정 1" in mail.subject
    assert "확인 필요 1" in mail.subject
    assert "Rules" in mail.text and "Guides" in mail.text
    assert "Secret Study" not in mail.text + mail.html + mail.subject
    assert "회원 전용 출처 #6" in mail.text
    assert "<b>x</b>" not in mail.html and "&lt;b&gt;x&lt;/b&gt;" in mail.html


def test_daily_report_without_problems_has_no_problem_count_in_the_subject():
    mail = t.report_mail(TO, "2026-11-10", [t.ReportLine(4, "Rules", False, "ok")], URL)
    assert "확인 필요" not in mail.subject


def test_test_mail():
    mail = t.test_mail(TO, URL)
    assert "메일 설정" in mail.subject and "Mail settings test" in mail.subject
