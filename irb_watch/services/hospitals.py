"""Hospitals: a platform-wide catalog that groups boards (sources).

Rules (owner, 2026-11-12; see docs/decisions.md "Hospitals"):
  * Edited by the platform admin only. Organizations never own a hospital.
  * Every signed-in user may read the list (names only): any hospital may be added to a project
    and get a pipeline. Board documents stay gated per board.
  * Every board belongs to exactly one hospital (sources.hospital_id). A board's kind
    (BOARD_KINDS) only sets defaults; users still pick documents per pipeline step.
  * Names are unique, ignoring case and outer spaces. No delete or archive in this step.
"""

import logging
from dataclasses import dataclass
from typing import Protocol

from irb_watch.services.errors import ConflictError, NotFoundError, ValidationError
from irb_watch.services.users import AuditSink

log = logging.getLogger(__name__)

BOARD_KINDS = ("notice", "forms", "regulations", "other")
MAX_NAME_CHARS = 100
MAX_NOTE_CHARS = 1000


@dataclass(frozen=True)
class Hospital:
    id: int
    name: str
    note: str = ""
    created_at: str = ""
    updated_at: str = ""


class DuplicateHospitalError(Exception):
    """Raised by the store when another hospital has the same name (any case)."""


class HospitalStore(Protocol):
    def list(self) -> list[Hospital]: ...
    def get(self, hospital_id: int) -> Hospital | None: ...
    def create(self, name: str, note: str, created_by: str | None) -> Hospital: ...
    def update(self, hospital_id: int, values: dict) -> Hospital: ...
    def delete(self, hospital_id: int) -> None:
        """Remove a hospital with no boards (the database refuses one that has any)."""
        ...


def checked_name(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise ValidationError("the hospital name is empty")
    if len(name) > MAX_NAME_CHARS:
        raise ValidationError(f"the hospital name may have at most {MAX_NAME_CHARS} characters")
    return name


def checked_kind(kind: str) -> str:
    if kind not in BOARD_KINDS:
        raise ValidationError(f"board_kind must be one of: {', '.join(BOARD_KINDS)}")
    return kind


class HospitalService:
    def __init__(self, store: HospitalStore, audit: AuditSink) -> None:
        self._store = store
        self._audit = audit

    def list(self) -> list[Hospital]:
        return sorted(self._store.list(), key=lambda h: h.name.lower())

    def get(self, hospital_id: int) -> Hospital:
        found = self._store.get(hospital_id)
        if found is None:
            raise NotFoundError("hospital not found")
        return found

    def find_by_name(self, name: str) -> Hospital | None:
        """Same name, ignoring case and any run of spaces (in both names)."""
        wanted = " ".join(name.split()).lower()
        return next(
            (h for h in self._store.list() if " ".join(h.name.split()).lower() == wanted), None
        )

    def remove_empty(self, actor_id: str | None, hospital_id: int) -> None:
        """Undo a hospital made for a board that was then not created. Best effort: a hospital
        left behind is only an empty row an admin can reuse; the failure is logged."""
        try:
            self._store.delete(hospital_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not remove empty hospital %s: %s", hospital_id, type(exc).__name__)
            return
        self._audit.record(
            "admin", "hospital.removed", actor_id,
            title="Hospital removed again (its board was not created)",
            payload={"hospital_id": hospital_id},
        )

    def create(self, actor_id: str | None, name: str, note: str = "") -> Hospital:
        name = checked_name(name)
        note = note.strip()
        if len(note) > MAX_NOTE_CHARS:
            raise ValidationError(f"the note may have at most {MAX_NOTE_CHARS} characters")
        try:
            hospital = self._store.create(name, note, actor_id)
        except DuplicateHospitalError as exc:
            raise ConflictError("a hospital with this name already exists") from exc
        self._audit.record(
            "admin", "hospital.created", actor_id,
            title=f"Hospital {name} added", payload={"hospital_id": hospital.id},
        )
        return hospital

    def update(
        self, actor_id: str | None, hospital_id: int, name: str | None = None, note: str | None = None
    ) -> Hospital:
        current = self.get(hospital_id)
        values: dict = {}
        if name is not None and checked_name(name) != current.name:
            values["name"] = checked_name(name)
        if note is not None and note.strip() != current.note:
            if len(note.strip()) > MAX_NOTE_CHARS:
                raise ValidationError(f"the note may have at most {MAX_NOTE_CHARS} characters")
            values["note"] = note.strip()
        if not values:
            return current
        try:
            updated = self._store.update(hospital_id, values)
        except DuplicateHospitalError as exc:
            raise ConflictError("a hospital with this name already exists") from exc
        self._audit.record(
            "admin", "hospital.updated", actor_id,
            title=f"Hospital {updated.name} updated",
            payload={"hospital_id": hospital_id, **{k: {"from": getattr(current, k), "to": v} for k, v in values.items()}},
        )
        return updated
