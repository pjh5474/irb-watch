"""The figures of the weekly stats mail, from raw rows. No I/O.

What the data can say:
  * summaries  = audit events summary.requested (a user's first summary of a version) and
                 summary.regenerated (an admin's re-summary), each with the acting user;
  * downloads  = audit events file.download_link, with the acting user and the source;
  * documents  = change cards document.new / reappeared (counted as new) and document.updated;
  * scan health = scan.run_finished payload sums (scanned, failed, skipped);
  * "active" users = accounts that SIGNED IN during the window. Supabase keeps only the LAST
    sign-in date, so this cannot be compared with the previous week (a user who signed in
    again this week no longer shows a date from last week) and sign-ins cannot be counted.
Page views and searches are not recorded anywhere.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

SUMMARY_EVENTS = frozenset({"summary.requested", "summary.regenerated"})
DOWNLOAD_EVENT = "file.download_link"
NEW_DOC_EVENTS = frozenset({"document.new", "document.reappeared"})
UPDATED_DOC_EVENT = "document.updated"
SCAN_RUN_EVENT = "scan.run_finished"

# Everything the store has to fetch for the two windows.
WANTED_EVENTS = sorted(
    {*SUMMARY_EVENTS, DOWNLOAD_EVENT, *NEW_DOC_EVENTS, UPDATED_DOC_EVENT, SCAN_RUN_EVENT}
)

MAX_USER_ROWS = 50


@dataclass(frozen=True)
class Window:
    """A half-open time window [start, end)."""

    start: datetime
    end: datetime

    def contains(self, moment: datetime | None) -> bool:
        return moment is not None and self.start <= moment < self.end

    def previous(self) -> "Window":
        return Window(self.start - (self.end - self.start), self.start)

    @staticmethod
    def last_days(end: datetime, days: int = 7) -> "Window":
        return Window(end - timedelta(days=days), end)


@dataclass(frozen=True)
class Event:
    type: str
    actor_id: str | None
    source_id: int | None
    at: datetime
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class UserInfo:
    id: str
    email: str | None
    created_at: datetime | None
    last_sign_in_at: datetime | None


@dataclass(frozen=True)
class DocCounts:
    new: int = 0
    updated: int = 0


@dataclass(frozen=True)
class Period:
    summaries: int
    downloads: int
    downloads_by_source: dict[int, int]
    docs_by_source: dict[int, DocCounts]
    scan_runs: int
    scans: int
    scans_failed: int
    scans_skipped: int


@dataclass(frozen=True)
class UserRow:
    email: str
    last_sign_in_at: datetime | None
    summaries: int
    downloads: int


def period(window: Window, events: list[Event]) -> Period:
    summaries = downloads = scan_runs = scans = failed = skipped = 0
    downloads_by_source: dict[int, int] = {}
    docs: dict[int, DocCounts] = {}
    for event in events:
        if not window.contains(event.at):
            continue
        if event.type in SUMMARY_EVENTS:
            summaries += 1
        elif event.type == DOWNLOAD_EVENT:
            downloads += 1
            if event.source_id is not None:
                downloads_by_source[event.source_id] = downloads_by_source.get(event.source_id, 0) + 1
        elif event.type in NEW_DOC_EVENTS and event.source_id is not None:
            current = docs.get(event.source_id, DocCounts())
            docs[event.source_id] = DocCounts(current.new + 1, current.updated)
        elif event.type == UPDATED_DOC_EVENT and event.source_id is not None:
            current = docs.get(event.source_id, DocCounts())
            docs[event.source_id] = DocCounts(current.new, current.updated + 1)
        elif event.type == SCAN_RUN_EVENT:
            scan_runs += 1
            scans += _int(event.payload.get("scanned"))
            failed += _int(event.payload.get("failed"))
            skipped += _int(event.payload.get("skipped"))
    return Period(summaries, downloads, downloads_by_source, docs, scan_runs, scans, failed, skipped)


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def signed_in(window: Window, users: list[UserInfo]) -> int:
    """Accounts whose last sign-in falls in the window."""
    return sum(1 for u in users if window.contains(u.last_sign_in_at))


def new_accounts(window: Window, users: list[UserInfo]) -> int:
    return sum(1 for u in users if window.contains(u.created_at))


def user_rows(
    window: Window, events: list[Event], users: list[UserInfo], cap: int = MAX_USER_ROWS
) -> tuple[list[UserRow], int]:
    """Users with any activity in the window: a summary, a download or a sign-in. Sorted by
    summaries + downloads, then by the latest sign-in; at most `cap` rows. Returns the rows and
    how many more there were."""
    summaries: dict[str, int] = {}
    downloads: dict[str, int] = {}
    for event in events:
        if event.actor_id is None or not window.contains(event.at):
            continue
        if event.type in SUMMARY_EVENTS:
            summaries[event.actor_id] = summaries.get(event.actor_id, 0) + 1
        elif event.type == DOWNLOAD_EVENT:
            downloads[event.actor_id] = downloads.get(event.actor_id, 0) + 1

    rows = []
    for user in users:
        if not user.email:
            continue
        s, d = summaries.get(user.id, 0), downloads.get(user.id, 0)
        if s == 0 and d == 0 and not window.contains(user.last_sign_in_at):
            continue
        rows.append(UserRow(user.email, user.last_sign_in_at, s, d))
    rows.sort(
        key=lambda r: (
            -(r.summaries + r.downloads),
            -(r.last_sign_in_at.timestamp() if r.last_sign_in_at else 0),
            r.email,
        )
    )
    return rows[:cap], max(0, len(rows) - cap)
