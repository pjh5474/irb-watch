"""Daily scan schedule (APScheduler), started with the API process.

  * daily run: SCAN_CRON in SCAN_TIMEZONE (default 06:00 Asia/Seoul);
  * startup catch-up: once, shortly after start, for sources whose last saved scan is over
    24 hours old (covers a server that was down at run time);
  * one later retry per source whose scan failed only because the site was unavailable.

Off unless SCHEDULER_ENABLED=true, so tests and local runs never start scans by themselves.
With several server instances every instance would try; the database scan lock makes the
others skip, with the reason recorded.
"""

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from irb_watch.services.runner import CATCH_UP, SCHEDULED, ScanRunner

log = logging.getLogger(__name__)

CATCH_UP_DELAY = timedelta(seconds=15)
MISFIRE_GRACE_SECONDS = 3600


class ScanScheduler:
    def __init__(self, runner: ScanRunner, cron: str, timezone: str) -> None:
        self._runner = runner
        self._tz = ZoneInfo(timezone)
        self._cron = cron
        self._scheduler = BackgroundScheduler(timezone=self._tz)

    def start(self) -> None:
        self._scheduler.add_job(
            self._runner.run_all,
            CronTrigger.from_crontab(self._cron, timezone=self._tz),
            args=[SCHEDULED],
            id="daily-scan",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
            replace_existing=True,
        )
        self._scheduler.add_job(
            self._runner.run_all,
            DateTrigger(run_date=datetime.now(UTC) + CATCH_UP_DELAY),
            args=[CATCH_UP],
            id="startup-catch-up",
            max_instances=1,
            replace_existing=True,
        )
        self._scheduler.start()
        log.info("scan scheduler started: cron %r in %s", self._cron, self._tz.key)

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def schedule_retry(self, source_id: int, due: datetime) -> None:
        """One-off retry of a single source; replaces any earlier retry of the same source."""
        self._scheduler.add_job(
            self._runner.run_retry,
            DateTrigger(run_date=due),
            args=[source_id],
            id=f"retry-{source_id}",
            replace_existing=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )

    def job_ids(self) -> list[str]:
        return sorted(job.id for job in self._scheduler.get_jobs())


def next_daily_run(cron: str, timezone: str, now: datetime | None = None) -> datetime | None:
    """When the daily run fires next (for tests and for showing to people)."""
    tz = ZoneInfo(timezone)
    trigger = CronTrigger.from_crontab(cron, timezone=tz)
    now = (now or datetime.now(UTC)).astimezone(tz)
    return trigger.get_next_fire_time(None, now)
