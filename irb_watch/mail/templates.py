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
