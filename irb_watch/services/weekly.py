"""The weekly stats mail for admins (usage and health of the last 7 days).

  * Runs from the scheduler (Mondays 07:00 in SCAN_TIMEZONE by default), once per ISO week: a
    claim key `weekly:<year>-W<week>` in mail_claims stops a second mail, and a failed send
    releases the claim so a later run in the same week may try again.
  * Goes to all active admins. The per-user table names accounts by email (admin-only, the
    owner's choice); members-only sources appear only as "members-only source #id".
  * Plain server code, no AI. A problem here is logged and audited, never raised into the
    scheduler. `preview()` sends the same report to ONE address (the caller's own) without
    taking the week's claim, so it can be looked at before Monday.
  * See core/weekly.py for what the data can and cannot say (only the LAST sign-in date exists).
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from irb_watch.core import health as h
from irb_watch.core import weekly as w
from irb_watch.mail import templates as t
from irb_watch.mail.client import Mailer, MailError
from irb_watch.services.sources import Source
from irb_watch.services.users import AuditSink

log = logging.getLogger(__name__)


class WeeklyStore(Protocol):
    def events(self, types: list[str], start: datetime, end: datetime) -> list[w.Event]: ...
    def users(self) -> list[w.UserInfo]: ...
    def claim(self, key: str) -> bool: ...
    def release(self, key: str) -> None: ...
    def admin_emails(self) -> tuple[str, ...]: ...


class WeeklyStatsService:
    def __init__(
        self,
        store: WeeklyStore,
        mailer: Mailer | None,
        audit: AuditSink,
        list_sources: Callable[[], list[Source]],
        *,
        app_url: str = "",
        stale_hours: int = 36,
        timezone: str = "Asia/Seoul",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._mailer = mailer
        self._audit = audit
        self._list_sources = list_sources
        self._app_url = app_url.rstrip("/")
        self._stale_hours = stale_hours
        self._tz = ZoneInfo(timezone)
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self._mailer is not None and bool(self._app_url)

    # -- entry points ----------------------------------------------------------------------------

    def send_scheduled(self) -> bool:
        """The weekly job. True if the mail was sent. Never raises."""
        if not self.enabled or self._mailer is None:
            return False
        now = self._clock()
        local = now.astimezone(self._tz).isocalendar()
        key = f"weekly:{local.year}-W{local.week:02d}"
        try:
            if not self._store.claim(key):
                return False  # already sent this week
        except Exception:
            log.exception("could not claim the weekly report")
            return False
        sent = False
        try:
            to = self._store.admin_emails()
            if not to:
                self._audit.record(
                    "admin", "mail.skipped", None, title="Mail (weekly) skipped: no admin address",
                    payload={"kind": "weekly_stats"},
                )
            else:
                report = self._report(now)
                self._mailer.send(t.weekly_mail(to, report, self._admin_url()))
                self._audit.record(
                    "admin", "mail.sent", None, title="Mail (weekly_stats) sent",
                    payload={"kind": "weekly_stats", "recipients": len(to), "week": key},
                )
                sent = True
        except MailError as exc:
            log.warning("weekly mail failed: %s", exc)
            self._audit.record(
                "admin", "mail.failed", None, title="Mail (weekly_stats) failed",
                payload={"kind": "weekly_stats", "error": str(exc)},
            )
        except Exception:
            log.exception("the weekly report crashed")
        if not sent:
            try:
                self._store.release(key)  # a later run in this week may try again
            except Exception:
                log.exception("could not release the weekly claim")
        return sent

    def preview(self, to: str) -> None:
        """The report for the last 7 days to ONE address, without the week's claim."""
        if not self.enabled or self._mailer is None:
            raise MailError("email is not set up on this server")
        report = self._report(self._clock())
        self._mailer.send(t.weekly_mail((to,), report, self._admin_url(), preview=True))

    # -- building the report -----------------------------------------------------------------------

    def _admin_url(self) -> str:
        return f"{self._app_url}/ko/admin/sources"

    def _date(self, moment: datetime | None) -> str:
        return moment.astimezone(self._tz).date().isoformat() if moment else "-"

    def _report(self, now: datetime) -> t.WeeklyReport:
        window = w.Window.last_days(now)
        before = window.previous()
        events = self._store.events(w.WANTED_EVENTS, before.start, window.end)
        users = self._store.users()
        sources = {s.id: s for s in self._list_sources()}
        this, last = w.period(window, events), w.period(before, events)

        def ws(source_id: int) -> t.WeeklySource:
            s = sources.get(source_id)
            return t.WeeklySource(source_id, s.name if s else "", s.requires_login if s else True)

        rows, hidden = w.user_rows(window, events, users)
        unhealthy = []
        for s in sorted(sources.values(), key=lambda s: s.id):
            health = h.source_health(
                is_active=s.is_active,
                last_scan_at=s.last_scan_at,
                last_scan_status=s.last_scan_status,
                scan_in_progress=s.scan_started_at is not None,
                scans_paused_reason=s.scans_paused_reason,
                now=now,
                stale_hours=self._stale_hours,
                manual=s.is_manual,
            )
            if health.alarming:
                unhealthy.append((ws(s.id), health.level))
        return t.WeeklyReport(
            period_text=f"{self._date(window.start)} ~ {self._date(window.end)}",
            signed_in=w.signed_in(window, users),
            new_accounts=w.new_accounts(window, users),
            summaries=this.summaries,
            summaries_before=last.summaries,
            downloads=this.downloads,
            downloads_before=last.downloads,
            users=tuple(
                t.WeeklyUser(r.email, self._date(r.last_sign_in_at), r.summaries, r.downloads)
                for r in rows
            ),
            users_hidden=hidden,
            docs=tuple(
                (ws(sid), c.new, c.updated)
                for sid, c in sorted(this.docs_by_source.items())
                if c.new + c.updated > 0
            ),
            scan_runs=this.scan_runs,
            scans=this.scans,
            scans_failed=this.scans_failed,
            scans_skipped=this.scans_skipped,
            unhealthy=tuple(unhealthy),
            downloads_by_source=tuple(
                (ws(sid), count)
                for sid, count in sorted(this.downloads_by_source.items(), key=lambda p: -p[1])
            ),
        )
