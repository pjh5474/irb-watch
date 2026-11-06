"""Runs scans for many sources: the daily run, the startup catch-up, retries, "run all now".

Rules (decided with the owner, see docs/decisions.md):
  * one worker per site (host); sites run in parallel, boards of one site run one by one,
    so a problem on one site never stops another and the hospital sees one polite client;
  * a source whose scan is still running is skipped and the reason is recorded;
  * a source paused after a rejected login is skipped (no automatic login retries);
  * a scan that failed only because the site was unavailable is retried once, 1 hour later;
  * the startup catch-up scans sources whose last saved scan is older than 24 hours.
Every skip and every run leaves an audit entry, so nothing looks like "no change".
"""

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit

from irb_watch.services.errors import ConflictError, NotFoundError, ServiceError
from irb_watch.services.scan import FAILED, OK, ScanSummary
from irb_watch.services.sources import Source
from irb_watch.services.users import AuditSink

log = logging.getLogger(__name__)

SCHEDULED = "scheduled"
CATCH_UP = "catch_up"
RETRY = "retry"
MANUAL = "manual"

CATCH_UP_AFTER = timedelta(hours=24)
RETRY_AFTER = timedelta(hours=1)
MAX_SITE_WORKERS = 8


class Scans(Protocol):
    def begin_scan(self, source_id: int) -> Source: ...
    def run_scan(self, source: Source, actor_id: str | None) -> ScanSummary: ...


@dataclass
class RunReport:
    trigger: str
    scanned: dict[int, str] = field(default_factory=dict)  # source id -> final status
    skipped: dict[int, str] = field(default_factory=dict)  # source id -> reason
    retries_planned: list[int] = field(default_factory=list)


def site_of(source: Source) -> str:
    return (urlsplit(source.url).hostname or source.url).lower()


class ScanRunner:
    def __init__(
        self,
        list_sources: Callable[[], list[Source]],
        get_source: Callable[[int], Source | None],
        scans: Scans,
        audit: AuditSink,
        schedule_retry: Callable[[int, datetime], None],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._list_sources = list_sources
        self._get_source = get_source
        self._scans = scans
        self._audit = audit
        self._schedule_retry = schedule_retry
        self._clock = clock
        self._run_lock = threading.Lock()

    # -- entry points ----------------------------------------------------------------

    def is_running(self) -> bool:
        return self._run_lock.locked()

    def run_all(self, trigger: str = SCHEDULED, actor_id: str | None = None) -> RunReport:
        """Scan every active source (or, for CATCH_UP, the overdue ones).

        One run at a time: if another full run is going, this one is skipped and recorded.
        """
        report = RunReport(trigger)
        if not self._run_lock.acquire(blocking=False):
            report.skipped[0] = "another full run is still in progress"
            self._audit.record(
                "scan",
                "scan.run_skipped",
                actor_id,
                title="Scan run skipped",
                payload={"trigger": trigger, "reason": report.skipped[0]},
            )
            return report
        try:
            now = self._clock()
            by_site: dict[str, list[Source]] = defaultdict(list)
            for source in self._list_sources():
                if not source.is_active:
                    continue
                if trigger == CATCH_UP and not self._is_overdue(source, now):
                    continue  # quietly: the catch-up only looks at overdue sources
                reason = self._skip_reason(source)
                if reason:
                    self._record_skip(source, trigger, reason, actor_id)
                    report.skipped[source.id] = reason
                    continue
                by_site[site_of(source)].append(source)

            if by_site:
                workers = min(len(by_site), MAX_SITE_WORKERS)
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scan-site") as pool:
                    for _ in pool.map(
                        lambda sources: self._run_site(sources, trigger, actor_id, report),
                        by_site.values(),
                    ):
                        pass
            self._audit.record(
                "scan",
                "scan.run_finished",
                actor_id,
                title=f"Scan run finished ({trigger})",
                payload={
                    "trigger": trigger,
                    "scanned": len(report.scanned),
                    "failed": sum(1 for s in report.scanned.values() if s == FAILED),
                    "skipped": len(report.skipped),
                    "retries_planned": len(report.retries_planned),
                },
            )
            return report
        finally:
            self._run_lock.release()

    def run_retry(self, source_id: int) -> RunReport:
        """The single later retry of a source whose scan failed because the site was down."""
        report = RunReport(RETRY)
        try:
            source = self._get_source(source_id)
            if source is not None:
                self._run_one(source, RETRY, None, report)
        except Exception:  # a planned retry must never fail silently
            log.exception("planned retry of source %s crashed", source_id)
            report.scanned[source_id] = FAILED
            self._audit.record(
                "scan",
                "scan.failed",
                None,
                title=f"Retry of source {source_id}: runner error",
                payload={"error": "the planned retry hit an unexpected error; see server log"},
                source_id=source_id,
            )
        return report

    # -- decisions ---------------------------------------------------------------------

    def _is_overdue(self, source: Source, now: datetime) -> bool:
        last = _parse(source.last_scan_at)
        return last is None or now - last >= CATCH_UP_AFTER

    def _skip_reason(self, source: Source) -> str | None:
        """Why a full run (any trigger) must not scan this source, or None."""
        if source.scans_paused_reason:
            return (
                f"paused after a rejected login ({source.scans_paused_reason}); "
                "start a scan of this source by hand to try again"
            )
        return None

    # -- work --------------------------------------------------------------------------

    def _run_site(
        self, sources: list[Source], trigger: str, actor_id: str | None, report: RunReport
    ) -> None:
        for source in sources:
            try:
                self._run_one(source, trigger, actor_id, report)
            except Exception:  # one source must never stop the rest of its site
                log.exception("scan runner crashed on source %s", source.id)
                report.scanned[source.id] = FAILED
                self._audit.record(
                    "scan",
                    "scan.failed",
                    actor_id,
                    title=f"Scan of {source.name}: runner error",
                    payload={"error": "the scan runner hit an unexpected error; see server log"},
                    source_id=source.id,
                )

    def _run_one(
        self, source: Source, trigger: str, actor_id: str | None, report: RunReport
    ) -> None:
        # The list was read when the run started; a site's later boards can wait minutes.
        # Look again so a source paused or deactivated meanwhile is not scanned.
        fresh = self._get_source(source.id)
        if fresh is None or not fresh.is_active:
            report.skipped[source.id] = "source was removed or deactivated"
            return
        source = fresh
        reason = self._skip_reason(source)
        if reason:
            self._record_skip(source, trigger, reason, actor_id)
            report.skipped[source.id] = reason
            return
        try:
            claimed = self._scans.begin_scan(source.id)
        except ConflictError as exc:
            self._record_skip(source, trigger, exc.message, actor_id)
            report.skipped[source.id] = exc.message
            return
        except NotFoundError as exc:
            report.skipped[source.id] = exc.message
            return
        except ServiceError as exc:
            self._record_skip(source, trigger, exc.message, actor_id)
            report.skipped[source.id] = exc.message
            return

        summary = self._scans.run_scan(claimed, actor_id)
        report.scanned[source.id] = summary.status
        if summary.status != OK and summary.retryable and trigger != RETRY:
            due = self._clock() + RETRY_AFTER
            self._schedule_retry(source.id, due)
            report.retries_planned.append(source.id)
            self._audit.record(
                "scan",
                "scan.retry_planned",
                actor_id,
                title=f"Scan of {source.name}: retry planned",
                payload={"at": due.isoformat(), "reason": summary.error},
                source_id=source.id,
            )

    def _record_skip(
        self, source: Source, trigger: str, reason: str, actor_id: str | None
    ) -> None:
        self._audit.record(
            "scan",
            "scan.skipped",
            actor_id,
            title=f"Scan of {source.name} skipped",
            payload={"trigger": trigger, "reason": reason},
            source_id=source.id,
        )


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
