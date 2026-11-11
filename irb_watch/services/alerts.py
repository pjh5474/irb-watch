"""Email alarms and the daily scan report for admins.

  * A source turning amber or red -> one alarm mail. Going from amber to red -> one more.
    Coming back to healthy after an alarm -> one recovery mail. Nothing on every scan.
  * The daily report: only after the SCHEDULED run, once per day (a claim key stops a second
    mail if the run fires twice).
  * State is saved only AFTER a successful send, so a failed send is tried again at the next
    evaluation and nothing is mailed twice.
  * A mail problem never fails a scan or a run: it is logged and audited as mail.failed.
  * Without an API key, sender and app address the service does nothing (no state changes), so
    problems that exist when mail is switched on are alarmed at the first evaluation.
Plain server code: no AI model is involved in any mail.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from irb_watch.core import health as h
from irb_watch.mail import templates as t
from irb_watch.mail.client import Mail, Mailer, MailError
from irb_watch.services.runner import SCHEDULED, RunReport
from irb_watch.services.sources import Source
from irb_watch.services.users import AuditSink

log = logging.getLogger(__name__)


class DigestSender(Protocol):
    def send_all(self) -> dict[str, int]: ...


class AlertStore(Protocol):
    def get_levels(self) -> dict[int, str]: ...
    def set_level(self, source_id: int, level: str, reason: str | None, at: str) -> None: ...
    def clear_level(self, source_id: int) -> None: ...
    def claim(self, key: str) -> bool: ...
    def release(self, key: str) -> None: ...
    def admin_emails(self) -> tuple[str, ...]: ...


class AlertService:
    def __init__(
        self,
        store: AlertStore,
        mailer: Mailer | None,
        audit: AuditSink,
        list_sources: Callable[[], list[Source]],
        get_source: Callable[[int], Source | None],
        *,
        app_url: str = "",
        stale_hours: int = 36,
        timezone: str = "Asia/Seoul",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        digest: DigestSender | None = None,
    ) -> None:
        self._digest = digest
        self._store = store
        self._mailer = mailer
        self._audit = audit
        self._list_sources = list_sources
        self._get_source = get_source
        self._app_url = app_url.rstrip("/")
        self._stale_hours = stale_hours
        self._tz = ZoneInfo(timezone)
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self._mailer is not None and bool(self._app_url)

    # -- entry points ------------------------------------------------------------------------

    def after_run(self, report: RunReport) -> None:
        """After a run: look at every source, then (scheduled runs only) send the daily report."""
        if not self.enabled:
            return
        sources = self._list_sources()
        self._evaluate_many(sources)
        if report.trigger == SCHEDULED:
            self._send_daily_report(report, sources)
            if self._digest is not None:
                try:
                    self._digest.send_all()
                except Exception:  # the digest must never fail a run or the admin report
                    log.exception("the daily digest failed")

    def evaluate_source(self, source_id: int) -> None:
        """After one scan of one source (a manual scan or a planned retry)."""
        if not self.enabled:
            return
        source = self._get_source(source_id)
        if source is not None:
            self._evaluate_many([source])

    def send_test(self, to: str) -> None:
        """A short mail to one address, to check the key, the sender domain and the DNS."""
        if not self.enabled or self._mailer is None:
            raise MailError("email is not set up on this server")
        self._mailer.send(t.test_mail((to,), self._admin_url()))

    # -- alarms ---------------------------------------------------------------------------------

    def _admin_url(self, source_id: int | None = None) -> str:
        path = f"/admin/sources/{source_id}" if source_id is not None else "/admin/sources"
        return f"{self._app_url}/ko{path}"

    def _health(self, source: Source) -> h.Health:
        return h.source_health(
            is_active=source.is_active,
            last_scan_at=source.last_scan_at,
            last_scan_status=source.last_scan_status,
            scan_in_progress=source.scan_started_at is not None,
            scans_paused_reason=source.scans_paused_reason,
            now=self._clock(),
            stale_hours=self._stale_hours,
            manual=source.is_manual,
        )

    def _evaluate_many(self, sources: list[Source]) -> None:
        try:
            notified = self._store.get_levels()
        except Exception:
            log.exception("could not read the alert state; skipping alarms this time")
            return
        for source in sources:
            try:
                self._evaluate(source, notified.get(source.id))
            except Exception:  # one source must not stop the others, and never fail a run
                log.exception("alarm evaluation crashed on source %s", source.id)

    def _evaluate(self, source: Source, notified: str | None) -> None:
        health = self._health(source)
        if health.level == h.SCANNING:
            return  # undecided until the scan finishes
        now = self._clock().isoformat()

        if health.alarming:
            if notified is None or h.RANK[health.level] > h.RANK[notified]:
                if self._send_alarm(source, health):
                    self._store.set_level(source.id, health.level, health.reason, now)
            elif h.RANK[health.level] < h.RANK[notified]:
                # Better than before but still not healthy: no mail, remember the new level so
                # getting worse again is mailed again.
                self._store.set_level(source.id, health.level, health.reason, now)
        elif notified is not None:
            if health.level == h.OK:
                if self._send_recovery(source):
                    self._store.clear_level(source.id)
            else:  # idle or switched off by an admin: nothing to announce
                self._store.clear_level(source.id)

    def _send_alarm(self, source: Source, health: h.Health) -> bool:
        info = t.AlertInfo(
            source_id=source.id,
            name=source.name,
            members_only=source.requires_login,
            level=health.level,
            reason=health.reason,
            error=source.last_scan_error,
            stale_hours=self._stale_hours,
            admin_url=self._admin_url(source.id),
        )
        return self._deliver(
            "alarm",
            lambda to: t.alarm_mail(to, info),
            source_id=source.id,
            payload={"level": health.level, "reason": health.reason},
        )

    def _send_recovery(self, source: Source) -> bool:
        url = self._admin_url(source.id)
        return self._deliver(
            "recovery",
            lambda to: t.recovery_mail(to, source.id, source.name, source.requires_login, url),
            source_id=source.id,
            payload={},
        )

    # -- the daily report --------------------------------------------------------------------

    def _send_daily_report(self, report: RunReport, sources: list[Source]) -> None:
        day = self._clock().astimezone(self._tz).date().isoformat()
        key = f"daily:{day}"
        try:
            if not self._store.claim(key):
                return  # already sent today
        except Exception:
            log.exception("could not claim the daily report; not sending it")
            return

        by_id = {s.id: s for s in sources}
        lines = []
        for source_id in sorted({*report.scanned, *report.skipped}):
            source = by_id.get(source_id)
            if source is None:
                continue
            summary = report.summaries.get(source_id)
            if source_id in report.scanned:
                status = report.scanned[source_id]
                note = summary.error if summary else None
            else:
                status, note = "skipped", report.skipped[source_id]
            lines.append(
                t.ReportLine(
                    source_id=source_id,
                    name=source.name,
                    members_only=source.requires_login,
                    status=status,
                    new=summary.new if summary else 0,
                    updated=summary.updated if summary else 0,
                    removed=summary.removed if summary else 0,
                    note=note,
                )
            )
        sent = self._deliver(
            "daily_report",
            lambda to: t.report_mail(to, day, lines, self._admin_url()),
            source_id=None,
            payload={"day": day, "sources": len(lines)},
        )
        if not sent:
            try:
                self._store.release(key)  # a later run today may try again
            except Exception:
                log.exception("could not release the daily report claim")

    # -- sending ----------------------------------------------------------------------------------

    def _deliver(
        self,
        kind: str,
        build: Callable[[tuple[str, ...]], Mail],
        *,
        source_id: int | None,
        payload: dict,
    ) -> bool:
        """Mail all admins. True only if a mail was really sent."""
        assert self._mailer is not None
        try:
            to = self._store.admin_emails()
        except Exception:
            log.exception("could not read the admin addresses")
            return False
        if not to:
            self._audit.record(
                "admin", "mail.skipped", None, title=f"Mail ({kind}) skipped: no admin address",
                payload={"kind": kind, **payload}, source_id=source_id,
            )
            return False
        try:
            self._mailer.send(build(to))
        except MailError as exc:
            log.warning("mail (%s) failed: %s", kind, exc)
            self._audit.record(
                "admin", "mail.failed", None, title=f"Mail ({kind}) failed",
                payload={"kind": kind, "error": str(exc), **payload}, source_id=source_id,
            )
            return False
        # Counts only: the audit log never holds an address.
        self._audit.record(
            "admin", "mail.sent", None, title=f"Mail ({kind}) sent",
            payload={"kind": kind, "recipients": len(to), **payload}, source_id=source_id,
        )
        return True
