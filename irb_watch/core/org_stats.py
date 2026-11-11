"""Monthly statistics per organization, from raw rows. No I/O.

What the data can say (see core/weekly.py for the same limits):
  * usage = audit events written by a user, each stamped with the user's organization when it
    was written (rows from before 2026-11-11 count as the Default organization), so moving a
    user does not change past numbers;
  * members = the organization's users now (deleted accounts not counted);
  * new accounts / signed in = by the user's organization NOW; Supabase keeps only the LAST
    sign-in date, so "signed in" means "last sign-in falls in the month" (a user who signed in
    during March and again in April counts only for April).
"""

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from irb_watch.core.weekly import UserInfo, Window

# metric -> the audit event types it counts
METRICS: dict[str, frozenset[str]] = {
    "chat_messages": frozenset({"agent.message"}),
    "summaries": frozenset({"summary.requested", "summary.regenerated"}),
    "drafts": frozenset({"docgen.generate"}),
    "revisions": frozenset({"docgen.revise"}),
    "exports": frozenset({"docgen.export"}),
    "downloads": frozenset({"file.download_link"}),
}
USAGE_EVENTS = sorted({t for types in METRICS.values() for t in types})
_METRIC_OF = {t: m for m, types in METRICS.items() for t in types}


def empty_counts() -> dict[str, int]:
    return dict.fromkeys(METRICS, 0)


@dataclass(frozen=True)
class UsageRow:
    organization_id: str
    actor_id: str
    event_type: str
    n: int


@dataclass(frozen=True)
class OrgRef:
    id: str
    name: str
    deleted: bool


@dataclass(frozen=True)
class OrgTotals:
    organization: OrgRef
    members: int
    new_accounts: int
    signed_in: int
    counts: dict[str, int]


@dataclass(frozen=True)
class UserTotals:
    user_id: str
    email: str | None
    member_now: bool  # False: the user has moved to another organization (or is deleted) since
    last_sign_in_at: datetime | None
    counts: dict[str, int] = field(default_factory=empty_counts)


class MonthError(ValueError):
    pass


def month_window(month: str, timezone: str, now: datetime) -> Window:
    """'YYYY-MM' as [first day 00:00, next month's first day 00:00) in `timezone`. A month after
    the current one is refused."""
    try:
        year_text, month_text = month.split("-")
        if len(year_text) != 4 or len(month_text) != 2:
            raise ValueError
        year, mon = int(year_text), int(month_text)
        if not 1 <= mon <= 12 or year < 2000:
            raise ValueError
    except ValueError as exc:
        raise MonthError("month must look like 2026-11") from exc
    tz = ZoneInfo(timezone)
    start = datetime(year, mon, 1, tzinfo=tz)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=tz)
    if start > now:
        raise MonthError("that month has not started yet")
    return Window(start, end)


def _add(counts: dict[str, int], event_type: str, n: int) -> None:
    metric = _METRIC_OF.get(event_type)
    if metric is not None and n > 0:
        counts[metric] += n


def org_totals(
    window: Window,
    orgs: list[OrgRef],
    membership: dict[str, str],
    accounts: list[UserInfo],
    usage: list[UsageRow],
) -> list[OrgTotals]:
    """One row per live organization, plus deleted ones that have usage in the window.
    `membership` maps user id -> organization id now; `accounts` are the live accounts."""
    counts: dict[str, dict[str, int]] = {}
    for row in usage:
        _add(counts.setdefault(row.organization_id, empty_counts()), row.event_type, row.n)
    members: dict[str, int] = {}
    new: dict[str, int] = {}
    signed: dict[str, int] = {}
    for account in accounts:
        org = membership.get(account.id)
        if org is None:
            continue
        members[org] = members.get(org, 0) + 1
        if window.contains(account.created_at):
            new[org] = new.get(org, 0) + 1
        if window.contains(account.last_sign_in_at):
            signed[org] = signed.get(org, 0) + 1
    rows = []
    for org in orgs:
        if org.deleted and org.id not in counts:
            continue
        rows.append(
            OrgTotals(
                org,
                members.get(org.id, 0),
                new.get(org.id, 0),
                signed.get(org.id, 0),
                counts.get(org.id, empty_counts()),
            )
        )
    return rows


def user_totals(
    organization_id: str,
    members_now: dict[str, str | None],
    accounts: list[UserInfo],
    usage: list[UsageRow],
) -> list[UserTotals]:
    """Per-user rows of one organization: every current member (even with no usage) and anyone
    whose usage in the window was stamped with this organization. `members_now` maps the
    organization's user ids now to their email. Sorted by total usage, then email."""
    by_id = {a.id: a for a in accounts}
    rows: dict[str, UserTotals] = {}

    def row(user_id: str) -> UserTotals:
        if user_id not in rows:
            account = by_id.get(user_id)
            email = members_now.get(user_id) or (account.email if account else None)
            rows[user_id] = UserTotals(
                user_id,
                email,
                user_id in members_now,
                account.last_sign_in_at if account else None,
            )
        return rows[user_id]

    for user_id in members_now:
        row(user_id)
    for usage_row in usage:
        if usage_row.organization_id == organization_id:
            _add(row(usage_row.actor_id).counts, usage_row.event_type, usage_row.n)
    return sorted(
        rows.values(),
        key=lambda r: (-sum(r.counts.values()), r.email or "~", r.user_id),
    )
