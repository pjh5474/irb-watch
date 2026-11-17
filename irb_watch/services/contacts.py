"""Hospital IRB contacts: phone, fax and email of a hospital's IRB office.

Rules (owner, 2026-11-17; see docs/decisions.md "Hospital contacts"):
  * Read from the hospital's R-BAY start page by the daily scan (once per hospital per run, inside
    the session the boards already use), or typed by a platform admin. A typed field wins on
    screen and the scan never changes it; clearing it shows the scanned text again.
  * The text is kept as the hospital wrote it (lines separated by a line break). It is untrusted
    page text: shown as plain text, wrapped as data when it goes to a model, never run as markup.
  * A failed check never changes the saved texts. It records a short kind (shown to every user)
    and the raw detail (admins only). A page with no cards is a failed check (R-BAY shows the
    three cards on every start page), never "no contact".
  * Every signed-in user may read it, also for a hospital whose boards have AI switched off, and
    the assistant may read it (contacts are short office numbers, not board documents).
  * Platform-wide, like `hospitals`: one hospital, one list for every customer.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from irb_watch.crawlers.errors import (
    LayoutChangedError,
    LoginRejectedError,
    SiteUnavailableError,
)
from irb_watch.services.errors import NotFoundError, ValidationError
from irb_watch.services.users import AuditSink

log = logging.getLogger(__name__)

FIELDS = ("phone", "fax", "email")
MAX_FIELD_CHARS = 500
MAX_DETAIL_CHARS = 500
KINDS = ("login_rejected", "site_unavailable", "layout_changed", "other")


@dataclass(frozen=True)
class HospitalContacts:
    hospital_id: int
    scanned: dict[str, str]  # field -> text read from R-BAY ("" = the hospital left it empty)
    manual: dict[str, str | None]  # field -> text an admin typed (None = not typed)
    check_status: str = "never"  # never | ok | failed
    checked_at: str | None = None
    last_ok_at: str | None = None
    error_kind: str | None = None
    error_detail: str | None = None  # admins only
    updated_at: str | None = None
    # The manager page: [{"tab", "entries": [{"title", "name", "email", "tel"}]}], read by the scan.
    staff: tuple[dict, ...] = ()
    staff_status: str = "never"  # never | none (no manager page) | ok | failed (staff = last good read)
    staff_error_kind: str | None = None
    staff_error_detail: str | None = None  # admins only

    def shown(self, field: str) -> tuple[str, str]:
        """(text, where it came from): 'admin', 'r-bay' or 'none' (nothing saved for it)."""
        typed = self.manual.get(field)
        if typed:
            return typed, "admin"
        text = self.scanned.get(field, "")
        return (text, "r-bay") if text else ("", "none")


def empty(hospital_id: int) -> HospitalContacts:
    return HospitalContacts(hospital_id, {f: "" for f in FIELDS}, {f: None for f in FIELDS})


def error_kind(exc: BaseException) -> str:
    """The short kind users see for a failed check."""
    if isinstance(exc, LoginRejectedError):
        return "login_rejected"
    if isinstance(exc, SiteUnavailableError):
        return "site_unavailable"
    if isinstance(exc, LayoutChangedError):
        return "layout_changed"
    return "other"


def staff_dicts(groups) -> list[dict]:
    """crawlers' StaffGroup list -> the plain JSON saved in the row."""
    return [
        {
            "tab": g.tab,
            "entries": [{"title": e.title, "name": e.name, "email": e.email, "tel": e.tel} for e in g.entries],
        }
        for g in groups
    ]


@dataclass(frozen=True)
class StaffRead:
    """The result of reading the manager page in one check."""

    present: bool  # the start page lists a manager page for this hospital
    groups: list[dict] | None = None  # the cards by tab, when it was read
    error: BaseException | None = None  # reading it failed


def _detail(error: BaseException) -> str:
    return " ".join(str(error).split())[:MAX_DETAIL_CHARS] or type(error).__name__


def after_check(
    stored: HospitalContacts, now: str, read: dict[str, str] | None, error: BaseException | None = None,
    staff: StaffRead | None = None,
) -> HospitalContacts:
    """The row after one check. `read` = the three texts the page gave. A failure keeps every
    saved text and records why; a typed (manual) text is never touched either way. `staff`: the
    manager page's result; None (not tried, e.g. the cards failed) leaves it as it was."""
    if error is not None:
        return replace(
            stored, check_status="failed", checked_at=now, error_kind=error_kind(error),
            error_detail=_detail(error),
        )
    assert read is not None
    out = replace(
        stored, scanned={f: read.get(f, "")[:MAX_FIELD_CHARS] for f in FIELDS}, check_status="ok",
        checked_at=now, last_ok_at=now, error_kind=None, error_detail=None,
    )
    if staff is None:
        return out
    if staff.error is not None:  # keep the last good staff, say why this read failed
        return replace(
            out, staff_status="failed", staff_error_kind=error_kind(staff.error),
            staff_error_detail=_detail(staff.error),
        )
    if not staff.present:
        if stored.staff:
            # A hospital that had a manager list and whose start page no longer links it: more
            # likely a changed or partial page than a removed page. Keep the list, say why.
            return replace(
                out, staff_status="failed", staff_error_kind="layout_changed",
                staff_error_detail="the start page no longer links the manager page",
            )
        return replace(out, staff=(), staff_status="none", staff_error_kind=None, staff_error_detail=None)
    return replace(
        out, staff=tuple(staff.groups or ()), staff_status="ok", staff_error_kind=None, staff_error_detail=None,
    )


class ContactStore(Protocol):
    def get(self, hospital_id: int) -> HospitalContacts | None: ...
    def save_scan(self, row: HospitalContacts) -> None:
        """Insert the row or update ONLY the columns the scan owns (scanned texts, the checks and
        the staff). The typed (manual) columns are never written here, so an admin edit made while
        a scan was running is not lost."""
        ...
    def save_manual(self, row: HospitalContacts, actor_id: str) -> None:
        """Insert the row or update ONLY the typed (manual) columns and who edited. The scan's
        columns are never written here."""
        ...
    def hospital_exists(self, hospital_id: int) -> bool: ...
    def checked_since(self, hospital_id: int, since: str) -> bool:
        """The hospital was checked (ok or failed) at or after `since`."""
        ...


class ContactService:
    def __init__(
        self,
        store: ContactStore,
        audit: AuditSink,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._audit = audit
        self._clock = clock

    def _now(self) -> str:
        return self._clock().isoformat()

    def get(self, hospital_id: int) -> HospitalContacts:
        """The saved contacts (an empty 'never checked' row when none exist). NotFoundError for
        a hospital that does not exist."""
        row = self._store.get(hospital_id)
        if row is not None:
            return row
        if not self._store.hospital_exists(hospital_id):
            raise NotFoundError("hospital not found")
        return empty(hospital_id)

    # -- the scan ----------------------------------------------------------------------------

    def should_check(self, hospital_id: int, min_hours: float = 12) -> bool:
        """False when the hospital was checked within `min_hours` (its boards share one run, so
        the cards are read once per run, not once per board)."""
        since = datetime.fromisoformat(self._now()).timestamp() - min_hours * 3600
        return not self._store.checked_since(hospital_id, datetime.fromtimestamp(since, UTC).isoformat())

    def record_check(
        self, hospital_id: int, read: dict[str, str] | None, error: BaseException | None = None,
        staff: StaffRead | None = None,
    ) -> None:
        """Save the result of one check. Never raises: a contact problem must not fail a scan."""
        try:
            stored = self._store.get(hospital_id) or empty(hospital_id)
            self._store.save_scan(after_check(stored, self._now(), read, error, staff))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not save contacts of hospital %s: %s", hospital_id, type(exc).__name__)

    # -- admin edit --------------------------------------------------------------------------

    def set_manual(self, actor_id: str, hospital_id: int, values: dict[str, str | None]) -> HospitalContacts:
        """Type or clear fields. A key present with text sets it, with None or blank clears it
        (the scanned text shows again); a key left out is unchanged."""
        unknown = set(values) - set(FIELDS)
        if unknown:
            raise ValidationError(f"unknown fields: {sorted(unknown)}")
        current = self.get(hospital_id)
        manual = dict(current.manual)
        for field, text in values.items():
            if text is not None:  # tidy like the scanned text: one space, no blank lines
                text = "\n".join(line for line in (" ".join(ln.split()) for ln in text.splitlines()) if line)
            if text is not None and len(text) > MAX_FIELD_CHARS:
                raise ValidationError(f"the {field} may have at most {MAX_FIELD_CHARS} characters")
            manual[field] = text or None
        if manual == current.manual:
            return current
        updated = replace(current, manual=manual)
        self._store.save_manual(updated, actor_id)
        self._audit.record(
            "admin", "hospital.contacts_edited", actor_id, title="Hospital contacts edited",
            payload={"hospital_id": hospital_id, "fields": sorted(values)},
        )
        return self.get(hospital_id)
