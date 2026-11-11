"""How healthy a source's scanning is, from fields the server keeps on `sources`.

The same rules as the frontend's src/lib/source-health.ts (the admin status light); keep the
two in step. A scan sets `last_scan_at` only when it saved its results, so after a failed scan
the date stays on the last good one and `last_scan_status` says what went wrong.

  problem    a person must act: login rejected (scans paused), or needs_review
  attention  the last scan failed (often temporary), or no good scan for the stale limit
  ok         the last scan saved, and recently
  scanning / idle (never scanned) / inactive are not alarms
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

OK = "ok"
SCANNING = "scanning"
ATTENTION = "attention"
PROBLEM = "problem"
IDLE = "idle"
INACTIVE = "inactive"

PAUSED = "paused"
NEEDS_REVIEW = "needs_review"
FAILED = "failed"
STALE = "stale"

# Severity order, to tell an escalation from a repeat.
RANK = {OK: 0, IDLE: 0, INACTIVE: 0, SCANNING: 0, ATTENTION: 1, PROBLEM: 2}


@dataclass(frozen=True)
class Health:
    level: str
    reason: str | None = None

    @property
    def alarming(self) -> bool:
        return self.level in (ATTENTION, PROBLEM)


def source_health(
    *,
    is_active: bool,
    last_scan_at: str | None,
    last_scan_status: str | None,
    scan_in_progress: bool = False,
    scans_paused_reason: str | None = None,
    now: datetime | None = None,
    stale_hours: int = 36,
    manual: bool = False,
) -> Health:
    """manual: a source an admin fills by hand (never scanned), so it is never "stale"; a
    document with a file warning still makes it a problem."""
    if not is_active:
        return Health(INACTIVE)
    if scan_in_progress:
        return Health(SCANNING)
    if scans_paused_reason:
        return Health(PROBLEM, PAUSED)
    if last_scan_status == "needs_review":
        return Health(PROBLEM, NEEDS_REVIEW)
    if last_scan_status == "failed":
        return Health(ATTENTION, FAILED)
    if last_scan_at is None:
        return Health(IDLE)
    if manual:
        return Health(OK)
    try:
        last = datetime.fromisoformat(last_scan_at)
    except ValueError:
        return Health(ATTENTION, STALE)  # an unreadable date is never "ok"
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    if (now or datetime.now(UTC)) - last > timedelta(hours=stale_hours):
        return Health(ATTENTION, STALE)
    return Health(OK)
