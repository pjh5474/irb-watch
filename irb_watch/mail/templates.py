"""The text of every mail: pure functions, Korean first and English below.

Rules:
  * every dynamic value is HTML-escaped (scan error text comes from a hospital web page and is
    untrusted) and cut to a safe length; subjects are one clean line;
  * a MEMBERS-ONLY source (a study) is shown as "members-only source #<id>" with no name and no
    scan error text: study names may be confidential and a mail reaches a third party. The admin
    page behind the link has the details.
"""

import html
import re
from dataclasses import dataclass

from irb_watch.core import health as h
from irb_watch.mail.client import Mail

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
MAX_ERROR_CHARS = 200

REASON_KO = {
    h.PAUSED: "로그인이 거부되어 수집이 중지되었습니다",
    h.NEEDS_REVIEW: "검토가 필요합니다 (로그인 또는 페이지 구조)",
    h.FAILED: "마지막 수집이 실패했습니다",
    h.STALE: "정상 수집이 오래 없었습니다 ({hours}시간 초과)",
}
REASON_EN = {
    h.PAUSED: "Scans are paused after a rejected login",
    h.NEEDS_REVIEW: "Needs review (login or page layout)",
    h.FAILED: "The last scan failed",
    h.STALE: "No good scan for over {hours} hours",
}


def one_line(value: str, limit: int = 120) -> str:
    """A safe single line: control characters (including newlines) become spaces, then cut."""
    return _CONTROL.sub(" ", value).strip()[:limit]


def source_label(source_id: int, name: str, members_only: bool, *, korean: bool) -> str:
    if members_only:
        return f"회원 전용 출처 #{source_id}" if korean else f"Members-only source #{source_id}"
    return one_line(name, 80) or f"#{source_id}"


def _reason(reason: str | None, hours: int, *, korean: bool) -> str:
    table = REASON_KO if korean else REASON_EN
    return table.get(reason or "", reason or "").format(hours=hours)


def _page(title: str, paragraphs: list[str], link_text: str, url: str) -> str:
    body = "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs if p)
    return (
        '<div style="font-family:sans-serif;font-size:14px;line-height:1.5;max-width:560px">'
        f"<h2 style=\"font-size:16px\">{html.escape(title)}</h2>{body}"
        f'<p><a href="{html.escape(url, quote=True)}">{html.escape(link_text)}</a></p>'
        "</div>"
    )


@dataclass(frozen=True)
class AlertInfo:
    source_id: int
    name: str
    members_only: bool
    level: str  # attention | problem
    reason: str | None
    error: str | None  # the last scan's error text, if any (untrusted)
    stale_hours: int
    admin_url: str


def alarm_mail(to: tuple[str, ...], a: AlertInfo) -> Mail:
    ko, en = (source_label(a.source_id, a.name, a.members_only, korean=k) for k in (True, False))
    severity_ko = "문제" if a.level == h.PROBLEM else "확인 필요"
    severity_en = "Problem" if a.level == h.PROBLEM else "Needs attention"
    reason_ko = _reason(a.reason, a.stale_hours, korean=True)
    reason_en = _reason(a.reason, a.stale_hours, korean=False)
    detail = "" if a.members_only or not a.error else one_line(a.error, MAX_ERROR_CHARS)

    subject = one_line(f"[IRB Watch] {severity_ko} · {ko} / {severity_en}: {en}", 140)
    lines_ko = [f"{ko}: {severity_ko}", reason_ko]
    lines_en = [f"{en}: {severity_en}", reason_en]
    if detail:
        lines_ko.append(f"수집 결과: {detail}")
        lines_en.append(f"Scan result: {detail}")
    link_ko, link_en = "관리자 화면에서 확인하기", "Open the admin page"
    text = "\n".join([*lines_ko, a.admin_url, "", "---", "", *lines_en, a.admin_url])
    page = (
        _page(f"{ko}: {severity_ko}", lines_ko[1:], link_ko, a.admin_url)
        + _page(f"{en}: {severity_en}", lines_en[1:], link_en, a.admin_url)
    )
    return Mail(to, subject, text, page)


def recovery_mail(to: tuple[str, ...], source_id: int, name: str, members_only: bool, admin_url: str) -> Mail:
    ko, en = (source_label(source_id, name, members_only, korean=k) for k in (True, False))
    subject = one_line(f"[IRB Watch] 복구됨 · {ko} / Recovered: {en}", 140)
    ko_line, en_line = f"{ko}: 다시 정상적으로 수집되었습니다.", f"{en}: scanning is healthy again."
    text = "\n".join([ko_line, admin_url, "", "---", "", en_line, admin_url])  # noqa: FLY002
    page = _page(ko_line, [], "관리자 화면", admin_url) + _page(en_line, [], "Admin page", admin_url)
    return Mail(to, subject, text, page)


KIND_KO = {"new": "새 문서", "updated": "수정됨", "reappeared": "다시 게시됨"}
KIND_EN = {"new": "New", "updated": "Updated", "reappeared": "Reappeared"}


@dataclass(frozen=True)
class DigestItem:
    document_id: int
    title: str
    kind: str  # new | updated | reappeared


@dataclass(frozen=True)
class DigestSource:
    source_id: int
    name: str
    members_only: bool
    count: int  # all changes of this source in the window, also the ones not listed
    items: tuple[DigestItem, ...] = ()  # listed items; always empty for a members-only source


@dataclass(frozen=True)
class DigestReminder:
    """One of the user's own IRB steps on a reminder day (phase A2). Names are typed by users:
    escaped and cut like every other value."""

    project: str
    hospital: str
    step: str
    due_on: str
    days_left: int  # negative = overdue
    url: str
    site: str = ""  # "01 · PI 김○○": tells two PIs of one hospital apart


def _when(days_left: int, *, korean: bool) -> str:
    if days_left < 0:
        return f"{-days_left}일 지남" if korean else f"{-days_left} day(s) overdue"
    if days_left == 0:
        return "오늘 마감" if korean else "due today"
    return f"{days_left}일 남음" if korean else f"due in {days_left} day(s)"


def _reminder_block(reminders: list[DigestReminder], *, korean: bool) -> tuple[list[str], list[str]]:
    head = f"IRB 단계 마감 알림: {len(reminders)}건" if korean else f"IRB step reminders: {len(reminders)}"
    text_lines, rows = [head], []
    for r in reminders:
        where = " · ".join(x for x in (one_line(r.project, 100), one_line(r.hospital, 80), one_line(r.site, 130)) if x)
        step = one_line(r.step, 100) or "-"
        when = f"{one_line(r.due_on, 10)} ({_when(r.days_left, korean=korean)})"
        text_lines.append(f"- {step} [{where}] {when} {r.url}")
        rows.append(
            f'<li><a href="{html.escape(r.url, quote=True)}">{html.escape(step)}</a> '
            f"[{html.escape(where)}] {html.escape(when)}</li>"
        )
    return text_lines, [f"<h2 style=\"font-size:16px\">{html.escape(head)}</h2><ul>{''.join(rows)}</ul>"]


def digest_mail(
    to: tuple[str, ...],
    day: str,
    sources: list[DigestSource],
    app_url: str,
    unsubscribe_page: str,
    unsubscribe_post: str,
    reminders: list[DigestReminder] | tuple[DigestReminder, ...] = (),
) -> Mail:
    """The daily mail of one user: IRB step reminders first (when any), then the new and updated
    documents (when any). Members-only sources (studies) are shown as a count only: their titles
    and names never go into a mail. One recipient per mail."""
    reminders = list(reminders)
    total = sum(s.count for s in sources)
    listed = sum(len(s.items) for s in sources)
    more = total - listed - sum(s.count for s in sources if s.members_only)
    settings_url = f"{app_url}/ko/settings"

    def block(korean: bool) -> tuple[list[str], list[str]]:
        text_lines, html_parts = [], []
        if reminders:
            r_text, r_html = _reminder_block(reminders, korean=korean)
            text_lines += [*r_text, ""]
            html_parts += r_html
        if not sources:
            return text_lines, html_parts
        n_text, n_html = _news_block(korean)
        return text_lines + n_text, html_parts + n_html

    def _news_block(korean: bool) -> tuple[list[str], list[str]]:
        kinds = KIND_KO if korean else KIND_EN
        head = (
            f"새 문서와 수정된 문서 ({day}): 모두 {total}건"
            if korean
            else f"New and updated documents ({day}): {total} in all"
        )
        text_lines, html_parts = [head], []
        for s in sources:
            label = source_label(s.source_id, s.name, s.members_only, korean=korean)
            if s.members_only:
                line = (
                    f"{label}: 볼 수 있는 회원 전용 출처에 변경 {s.count}건. 앱에서 확인하세요."
                    if korean
                    else f"{label}: {s.count} change(s) in a members-only source you can read. Open the app to see them."
                )
                text_lines.append(f"- {line}")
                html_parts.append(f"<p>{html.escape(line)}</p>")
                continue
            text_lines.append(f"{label} ({s.count})")
            rows = []
            for item in s.items:
                url = f"{app_url}/ko/documents/{item.document_id}"
                title = one_line(item.title, 200) or f"#{item.document_id}"
                text_lines.append(f"- [{kinds.get(item.kind, item.kind)}] {title} {url}")
                rows.append(
                    f"<li>[{html.escape(kinds.get(item.kind, item.kind))}] "
                    f'<a href="{html.escape(url, quote=True)}">{html.escape(title)}</a></li>'
                )
            html_parts.append(f"<p><strong>{html.escape(label)}</strong> ({s.count})</p><ul>{''.join(rows)}</ul>")
        if more > 0:
            tail = f"그 밖에 {more}건이 더 있습니다. 앱에서 확인하세요." if korean else f"{more} more. Open the app to see them."
            text_lines.append(tail)
            html_parts.append(f"<p>{html.escape(tail)}</p>")
        return text_lines, [f"<h2 style=\"font-size:16px\">{html.escape(head)}</h2>", *html_parts]

    ko_text, ko_html = block(True)
    en_text, en_html = block(False)
    foot_ko = "이 메일을 그만 받으려면:"
    foot_en = "To stop these emails:"
    text = "\n".join(
        [
            *ko_text, "", f"{foot_ko} {unsubscribe_page}", f"설정: {settings_url}",
            "", "---", "",
            *en_text, "", f"{foot_en} {unsubscribe_page}", f"Settings: {settings_url}",
        ]
    )
    foot_html = (
        f'<p style="font-size:12px;color:#666">{html.escape(foot_ko)} '
        f'<a href="{html.escape(unsubscribe_page, quote=True)}">수신 중지 / Unsubscribe</a> · '
        f'<a href="{html.escape(settings_url, quote=True)}">설정 / Settings</a></p>'
    )
    page = (
        '<div style="font-family:sans-serif;font-size:14px;line-height:1.5;max-width:560px">'
        + "".join(ko_html)
        + "<hr>"
        + "".join(en_html)
        + foot_html
        + "</div>"
    )
    if reminders and total:
        subject = one_line(
            f"[IRB Watch] IRB 단계 알림 {len(reminders)}건 · 새 문서·수정 {total}건"
            f" / {len(reminders)} IRB step reminder(s), {total} new or updated documents",
            200,
        )
    elif reminders:
        subject = one_line(
            f"[IRB Watch] IRB 단계 마감 알림 {len(reminders)}건 / {len(reminders)} IRB step reminder(s)"
        )
    else:
        subject = one_line(f"[IRB Watch] 새 문서·수정 {total}건 / {total} new or updated documents")
    headers = (
        ("List-Unsubscribe", f"<{unsubscribe_post}>"),
        ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click"),
    )
    return Mail(to, subject, text, page, headers)


@dataclass(frozen=True)
class WeeklySource:
    source_id: int
    name: str
    members_only: bool


@dataclass(frozen=True)
class WeeklyUser:
    email: str
    last_sign_in: str  # a date text or "-"
    summaries: int
    downloads: int


@dataclass(frozen=True)
class WeeklyReport:
    period_text: str  # for example "2026-11-03 ~ 2026-11-09"
    signed_in: int
    new_accounts: int
    summaries: int
    summaries_before: int
    downloads: int
    downloads_before: int
    users: tuple[WeeklyUser, ...]
    users_hidden: int
    docs: tuple[tuple[WeeklySource, int, int], ...]  # source, new, updated
    scan_runs: int
    scans: int
    scans_failed: int
    scans_skipped: int
    unhealthy: tuple[tuple[WeeklySource, str], ...]  # source and its level (attention | problem)
    downloads_by_source: tuple[tuple[WeeklySource, int], ...] = ()


def _trend(now: int, before: int) -> str:
    diff = now - before
    sign = "+" if diff > 0 else ""
    return f"{now} ({sign}{diff})"


def weekly_mail(to: tuple[str, ...], r: WeeklyReport, admin_url: str, *, preview: bool = False) -> Mail:
    """The weekly stats mail for admins. Korean first, English below. It names accounts by email
    (admin-only, the owner's choice); members-only sources are only ever "source #id"."""

    def label(s: WeeklySource, korean: bool) -> str:
        return source_label(s.source_id, s.name, s.members_only, korean=korean)

    def block(korean: bool) -> tuple[list[str], list[str]]:
        head = (
            f"주간 이용 현황 ({r.period_text})" if korean else f"Weekly usage report ({r.period_text})"
        )
        lines = [head]
        if preview:
            lines.append("(미리보기: 나에게만 보냈습니다)" if korean else "(Preview: sent only to you)")
        def k(ko: str, en: str) -> str:
            return ko if korean else en

        scans = (
            f"일일 실행 {r.scan_runs}회, 출처 수집 {r.scans}건, 실패 {r.scans_failed}건, 건너뜀 {r.scans_skipped}건"
            if korean
            else f"{r.scan_runs} daily runs, {r.scans} source scans, {r.scans_failed} failed, {r.scans_skipped} skipped"
        )
        compare = k("(지난 7일 대비)", "(vs the previous 7 days)")
        lines += [
            f"- {k('기간 중 로그인한 계정', 'Accounts that signed in')}: {r.signed_in}",
            f"- {k('새 계정', 'New accounts')}: {r.new_accounts}",
            f"- {k('AI 요약 요청', 'AI summaries requested')}: {_trend(r.summaries, r.summaries_before)} {compare}",
            f"- {k('파일 다운로드', 'File downloads')}: {_trend(r.downloads, r.downloads_before)} {compare}",
            f"- {k('수집', 'Scans')}: {scans}",
        ]
        if r.unhealthy:
            lines.append(k("지금 확인이 필요한 출처:", "Sources that need attention now:"))
            for s, level in r.unhealthy:
                tag = k("문제", "Problem") if level == "problem" else k("확인 필요", "Needs attention")
                lines.append(f"  - {label(s, korean)}: {tag}")
        if r.docs:
            lines.append(k("새 문서 / 수정된 문서 (출처별):", "New / updated documents per source:"))
            for s, new, updated in r.docs:
                lines.append(f"  - {label(s, korean)}: {new} / {updated}")
        if r.downloads_by_source:
            lines.append(k("다운로드 (출처별):", "Downloads per source:"))
            for s, count in r.downloads_by_source:
                lines.append(f"  - {label(s, korean)}: {count}")
        lines.append(k("이용한 계정:", "Accounts with activity:"))
        if r.users:
            for u in r.users:
                lines.append(
                    f"  - {one_line(u.email, 120)} | {k('마지막 로그인', 'last sign-in')} {u.last_sign_in} | "
                    f"{k('요약', 'summaries')} {u.summaries} | {k('다운로드', 'downloads')} {u.downloads}"
                )
            if r.users_hidden:
                lines.append(k(f"  그 밖에 {r.users_hidden}명", f"  and {r.users_hidden} more"))
        else:
            lines.append(k("  없음", "  none"))
        html_parts = [f"<h2 style=\"font-size:16px\">{html.escape(head)}</h2>"]
        html_parts += [f"<p>{html.escape(line.strip())}</p>" for line in lines[1:]]
        return lines, html_parts

    ko_text, ko_html = block(True)
    en_text, en_html = block(False)
    text = "\n".join([*ko_text, admin_url, "", "---", "", *en_text, admin_url])
    page = (
        '<div style="font-family:sans-serif;font-size:14px;line-height:1.5;max-width:640px">'
        + "".join(ko_html)
        + "<hr>"
        + "".join(en_html)
        + f'<p><a href="{html.escape(admin_url, quote=True)}">Admin page</a></p></div>'
    )
    subject = one_line(
        f"[IRB Watch] 주간 현황 · 로그인 {r.signed_in} · 요약 {r.summaries} · 다운로드 {r.downloads}"
        f" / Weekly report"
    )
    return Mail(to, subject, text, page)


def test_mail(to: tuple[str, ...], admin_url: str) -> Mail:
    ko = "이 메일은 IRB Watch의 메일 설정 확인용입니다. 받으셨다면 메일 발송이 정상입니다."
    en = "This mail checks the IRB Watch mail settings. If you can read it, sending works."
    text = f"{ko}\n\n{en}\n{admin_url}"
    page = _page("IRB Watch", [ko, en], "Admin page", admin_url)
    return Mail(to, "[IRB Watch] 메일 설정 확인 / Mail settings test", text, page)


@dataclass(frozen=True)
class ReportLine:
    source_id: int
    name: str
    members_only: bool
    status: str  # ok | needs_review | failed | skipped
    new: int = 0
    updated: int = 0
    removed: int = 0
    note: str | None = None  # why it was skipped, if it was


def report_mail(to: tuple[str, ...], day: str, lines: list[ReportLine], admin_url: str) -> Mail:
    problems = sum(1 for line in lines if line.status != "ok")
    total_new = sum(line.new for line in lines)
    total_updated = sum(line.updated for line in lines)

    def block(korean: bool) -> list[str]:
        out = []
        if korean:
            out.append(f"오늘의 수집 결과 ({day}): 새 문서 {total_new}건, 수정 {total_updated}건, 확인 필요 {problems}곳")
        else:
            out.append(f"Scan result for {day}: {total_new} new, {total_updated} updated, {problems} source(s) with a problem")
        for line in lines:
            label = source_label(line.source_id, line.name, line.members_only, korean=korean)
            mark = "OK" if line.status == "ok" else line.status
            counts = (
                f"새 {line.new} · 수정 {line.updated} · 삭제 {line.removed}"
                if korean
                else f"new {line.new} · updated {line.updated} · removed {line.removed}"
            )
            note = f" ({one_line(line.note, 100)})" if line.note and not line.members_only else ""
            out.append(f"- {label}: {mark} — {counts}{note}")
        return out

    ko_lines, en_lines = block(True), block(False)
    subject = one_line(
        f"[IRB Watch] 일일 수집 결과 {day}: 새 {total_new} · 수정 {total_updated}"
        + (f" · 확인 필요 {problems}" if problems else "")
        + " / Daily scan report"
    )
    text = "\n".join([*ko_lines, admin_url, "", "---", "", *en_lines, admin_url])
    page = (
        _page(ko_lines[0], ko_lines[1:], "관리자 화면", admin_url)
        + _page(en_lines[0], en_lines[1:], "Admin page", admin_url)
    )
    return Mail(to, subject, text, page)
