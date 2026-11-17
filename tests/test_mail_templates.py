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


def test_alarm_is_korean_only_with_the_exact_name_and_links():
    mail = t.alarm_mail(TO, info(site_url="https://www.r-bay.co.kr/agency/main/ABCDEFGH12?board=NOTI"))
    assert mail.to == TO
    assert "SNUH rules" in mail.subject and "문제" in mail.subject
    assert "Problem" not in mail.subject + mail.text and "---" not in mail.text  # no English half
    assert "로그인이 거부되어" in mail.text
    assert URL in mail.text and f'href="{URL}"' in mail.html
    assert "https://www.r-bay.co.kr/agency/main/ABCDEFGH12?board=NOTI" in mail.text
    assert 'href="https://www.r-bay.co.kr/agency/main/ABCDEFGH12?board=NOTI"' in mail.html


def test_the_site_link_only_takes_a_plain_web_address():
    for bad in ("javascript:alert(1)", 'https://x.kr/"onmouseover="x', "ftp://x.kr", "https://x.kr/a b", ""):
        mail = t.alarm_mail(TO, info(site_url=bad))
        assert "사이트" not in mail.text and "사이트 열기" not in mail.html


def test_attention_wording_and_the_stale_hours():
    mail = t.alarm_mail(TO, info(level=h.ATTENTION, reason=h.STALE, error=None, stale_hours=48))
    assert "확인 필요" in mail.subject
    assert "48시간" in mail.text


def test_the_scan_error_text_comes_from_the_web_and_is_escaped_and_cut():
    nasty = '<script>alert(1)</script>' + "x" * 500
    mail = t.alarm_mail(TO, info(error=nasty))
    assert "<script>" not in mail.html
    assert "&lt;script&gt;" in mail.html
    assert "x" * (t.MAX_ERROR_CHARS + 1) not in mail.text


def test_a_members_only_source_is_named_exactly_but_its_error_text_is_left_out():
    mail = t.alarm_mail(TO, info(name="가톨릭 : 공지", members_only=True))
    for part in (mail.subject, mail.text, mail.html):
        assert "wrong password" not in part
    assert "가톨릭 : 공지" in mail.subject and "가톨릭 : 공지" in mail.text
    assert "회원 전용 출처" not in mail.subject + mail.text


def test_subjects_are_one_clean_line_and_capped():
    mail = t.alarm_mail(TO, info(name="Line1\r\nBcc: evil@example.com\x00" + "y" * 400))
    assert "\n" not in mail.subject and "\r" not in mail.subject and "\x00" not in mail.subject
    assert len(mail.subject) <= 140


def test_recovery_mail_is_korean_only_with_the_name_and_links():
    mail = t.recovery_mail(TO, 5, "SNUH rules", False, URL, "https://hrpp.snuh.org/list?key=1")
    assert "복구됨" in mail.subject and "Recovered" not in mail.subject
    assert "SNUH rules" in mail.subject and "다시 정상적으로 수집" in mail.text and "---" not in mail.text
    assert URL in mail.text and "https://hrpp.snuh.org/list?key=1" in mail.text
    assert 'href="https://hrpp.snuh.org/list?key=1"' in mail.html
    named = t.recovery_mail(TO, 6, "Secret Study", True, URL)
    assert "Secret Study" in named.subject  # members-only ones are named too (owner, 2026-11-17)


def test_daily_report_is_korean_only_with_exact_names_and_counts_in_the_subject():
    lines = [
        t.ReportLine(4, "Rules", False, "ok", new=2, updated=1, removed=0),
        t.ReportLine(5, "Guides", False, "failed", note="site unavailable <b>x</b>"),
        t.ReportLine(6, "Secret Study", True, "ok", new=3),
    ]
    mail = t.report_mail(TO, "2026-11-10", lines, "https://app.example.com/ko/admin/sources")
    assert "2026-11-10" in mail.subject and "새 5" in mail.subject and "수정 1" in mail.subject
    assert "확인 필요 1" in mail.subject and "Daily scan report" not in mail.subject
    assert "Rules" in mail.text and "Guides" in mail.text
    assert "Secret Study" in mail.text and "회원 전용 출처" not in mail.text
    assert "실패" in mail.text and "정상" in mail.text and "---" not in mail.text
    assert "<b>x</b>" not in mail.html and "&lt;b&gt;x&lt;/b&gt;" in mail.html


def test_daily_report_lists_hospitals_whose_contacts_could_not_be_read():
    contacts = [
        t.ContactReportLine(3, "가톨릭대학교의료원", "layout_changed", None, "https://app.example.com/ko/hospitals/3"),
        t.ContactReportLine(8, "이화의대", "login_rejected", "site_unavailable", "https://app.example.com/ko/hospitals/8"),
    ]
    mail = t.report_mail(TO, "2026-11-10", [t.ReportLine(4, "Rules", False, "ok")], URL, contacts)
    assert "연락처 실패 2" in mail.subject
    assert "연락처를 읽지 못한 병원 2곳" in mail.text
    assert "- 가톨릭대학교의료원: 전화·팩스·이메일 읽기 실패 (페이지 구조가 바뀌었습니다)" in mail.text
    assert "담당자 목록 읽기 실패 (사이트에 연결하지 못했습니다)" in mail.text
    lines = mail.text.splitlines()
    first = next(i for i, ln in enumerate(lines) if ln.startswith("- 가톨릭대학교의료원"))
    assert lines[first + 1].strip() == "https://app.example.com/ko/hospitals/3"  # under its own hospital
    second = next(i for i, ln in enumerate(lines) if ln.startswith("- 이화의대"))
    assert lines[second + 1].strip() == "https://app.example.com/ko/hospitals/8"
    assert 'href="https://app.example.com/ko/hospitals/8"' in mail.html


def test_a_report_without_contact_problems_has_no_contact_part():
    mail = t.report_mail(TO, "2026-11-10", [t.ReportLine(4, "Rules", False, "ok")], URL, [])
    assert "연락처" not in mail.subject + mail.text + mail.html


def test_contact_text_in_the_report_is_escaped_and_one_line():
    contacts = [t.ContactReportLine(3, "<b>병원</b>\nBcc: x", "weird_kind", None, "https://app.example.com/ko/hospitals/3")]
    mail = t.report_mail(TO, "2026-11-10", [], URL, contacts)
    assert "<b>병원</b>" not in mail.html and "&lt;b&gt;" in mail.html
    assert "원인을 알 수 없는 오류" in mail.text  # an unknown kind has a plain wording
    assert "\n" not in mail.subject


def test_daily_report_without_problems_has_no_problem_count_in_the_subject():
    mail = t.report_mail(TO, "2026-11-10", [t.ReportLine(4, "Rules", False, "ok")], URL)
    assert "확인 필요" not in mail.subject


def test_test_mail():
    mail = t.test_mail(TO, URL)
    assert "메일 설정" in mail.subject and "Mail settings test" in mail.subject
