"""IRB pipeline, phase 1: the admin's setup (statuses, step types, default step order).

Rules (owner, 2026-11-11; see docs/decisions.md "IRB pipeline: admin setup"):
  * Server-wide settings, edited by platform admins only.
  * Statuses: a seeded list the admin may extend, rename, reorder, mark closed (finished) or
    archive. At least one open and one closed status always stay live; the first live open
    status is the starting status of a new step. Keys never change (steps store them).
  * Step types: name (unique among live ones), description, added AI instructions (at most
    4,000 characters, after the fixed safety rules), default guides and form template, input
    rules (earlier steps' files, the hospital's board documents), required documents. Archiving
    is refused while a default step order uses the type.
  * Default step order: one global list and optional per-hospital lists, 1 to 30 live step types,
    no repeats. A per-hospital list can be removed (the hospital then uses the global one); the
    global list cannot. A new pipeline copies the matching list (phase 2), so later edits do not
    change existing pipelines.
"""

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol

from irb_watch.services.errors import ConflictError, NotFoundError, ValidationError
from irb_watch.services.users import AuditSink

MAX_LABEL_CHARS = 60
MAX_NAME_CHARS = 100
MAX_DESCRIPTION_CHARS = 1000
MAX_INSTRUCTIONS_CHARS = 4000
MAX_REQUIRED_DOCS = 50
MAX_REQUIRED_DOC_CHARS = 200
MAX_GUIDES = 20
MAX_TEMPLATE_STEPS = 30
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


@dataclass(frozen=True)
class StepStatus:
    key: str
    label: str
    closed: bool
    position: int
    archived_at: str | None = None


@dataclass(frozen=True)
class StepType:
    id: int
    name: str
    description: str = ""
    instructions: str = ""
    guide_ids: tuple[int, ...] = ()
    form_template_id: int | None = None
    input_earlier_steps: bool = True
    input_board_documents: bool = False
    required_documents: tuple[str, ...] = ()
    archived_at: str | None = None
    updated_at: str = ""


@dataclass(frozen=True)
class StepTypeEdit:
    """A full set of a step type's editable values (create, or replace on update)."""

    name: str
    description: str = ""
    instructions: str = ""
    guide_ids: list[int] = field(default_factory=list)
    form_template_id: int | None = None
    input_earlier_steps: bool = True
    input_board_documents: bool = False
    required_documents: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PipelineTemplate:
    hospital_id: int | None  # None = the global default
    step_type_ids: tuple[int, ...]
    updated_at: str = ""


class NameTakenError(Exception):
    """Raised by the store when a live step type already has the name."""


class PipelineSetupStore(Protocol):
    def statuses(self) -> list[StepStatus]: ...
    def add_status(self, status: StepStatus) -> StepStatus: ...
    def update_status(self, key: str, values: dict) -> StepStatus: ...
    def step_types(self) -> list[StepType]: ...
    def get_step_type(self, type_id: int) -> StepType | None: ...
    def create_step_type(self, edit: StepTypeEdit, actor_id: str) -> StepType: ...
    def update_step_type(self, type_id: int, values: dict, actor_id: str) -> StepType: ...
    def templates(self) -> list[PipelineTemplate]: ...
    def save_template(self, hospital_id: int | None, step_type_ids: list[int], actor_id: str) -> PipelineTemplate: ...
    def delete_template(self, hospital_id: int) -> bool: ...
    def live_guide_ids(self, ids: list[int]) -> set[int]: ...
    def form_template_ready(self, template_id: int) -> bool: ...
    def hospital_exists(self, hospital_id: int) -> bool: ...


def _text(value: str, limit: int, what: str, *, required: bool = False) -> str:
    value = value.strip()
    if required and not value:
        raise ValidationError(f"the {what} is empty")
    if len(value) > limit:
        raise ValidationError(f"the {what} may have at most {limit:,} characters")
    return value


def _name(value: str, limit: int, what: str) -> str:
    return _text(" ".join(value.split()), limit, what, required=True)


class PipelineSetupService:
    def __init__(
        self,
        store: PipelineSetupStore,
        audit: AuditSink,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_key: Callable[[], str] = lambda: f"custom_{secrets.token_hex(4)}",
    ) -> None:
        self._store = store
        self._audit = audit
        self._clock = clock
        self._new_key = new_key

    def _record(self, actor_id: str, event: str, title: str, payload: dict) -> None:
        self._audit.record("admin", event, actor_id, title=title, payload=payload)

    # -- statuses -----------------------------------------------------------------------------

    def statuses(self, include_archived: bool = False) -> list[StepStatus]:
        rows = sorted(self._store.statuses(), key=lambda s: (s.position, s.key))
        return rows if include_archived else [s for s in rows if s.archived_at is None]

    def _status(self, key: str) -> StepStatus:
        found = next((s for s in self._store.statuses() if s.key == key), None)
        if found is None:
            raise NotFoundError("status not found")
        return found

    @staticmethod
    def _check_kinds(live: list[StepStatus]) -> None:
        if not any(not s.closed for s in live):
            raise ConflictError("at least one open (not finished) status must stay")
        if not any(s.closed for s in live):
            raise ConflictError("at least one closed (finished) status must stay")

    def _label_free(self, label: str, except_key: str | None = None) -> None:
        if any(
            s.archived_at is None and s.key != except_key and s.label.lower() == label.lower()
            for s in self._store.statuses()
        ):
            raise ConflictError("a status with this name already exists")

    def add_status(self, actor_id: str, label: str, closed: bool) -> StepStatus:
        label = _name(label, MAX_LABEL_CHARS, "status name")
        self._label_free(label)
        key = self._new_key()
        if not _KEY.match(key):
            raise ValueError(f"bad status key {key!r}")
        last = max((s.position for s in self._store.statuses()), default=0)
        status = self._store.add_status(StepStatus(key, label, closed, last + 1))
        self._record(actor_id, "pipeline.status_added", f"Status {label} added", {"key": key, "closed": closed})
        return status

    def update_status(
        self, actor_id: str, key: str, label: str | None = None, closed: bool | None = None
    ) -> StepStatus:
        current = self._status(key)
        if current.archived_at is not None:
            raise ConflictError("this status is archived; restore it first")
        values: dict = {}
        if label is not None:
            label = _name(label, MAX_LABEL_CHARS, "status name")
            if label != current.label:
                self._label_free(label, key)
                values["label"] = label
        if closed is not None and closed != current.closed:
            live = [replace(s, closed=closed) if s.key == key else s for s in self.statuses()]
            self._check_kinds(live)
            values["closed"] = closed
        if not values:
            return current
        updated = self._store.update_status(key, values)
        self._record(actor_id, "pipeline.status_updated", f"Status {updated.label} changed", {"key": key, **values})
        return updated

    def reorder_statuses(self, actor_id: str, keys: list[str]) -> list[StepStatus]:
        live = self.statuses()
        if sorted(keys) != sorted(s.key for s in live) or len(set(keys)) != len(keys):
            raise ValidationError("send every live status key exactly once")
        for position, key in enumerate(keys, start=1):
            self._store.update_status(key, {"position": position})
        self._record(actor_id, "pipeline.statuses_reordered", "Status order changed", {"keys": keys})
        return self.statuses()

    def archive_status(self, actor_id: str, key: str) -> StepStatus:
        current = self._status(key)
        if current.archived_at is not None:
            raise ConflictError("this status is already archived")
        self._check_kinds([s for s in self.statuses() if s.key != key])
        updated = self._store.update_status(key, {"archived_at": self._clock().isoformat()})
        self._record(actor_id, "pipeline.status_archived", f"Status {current.label} archived", {"key": key})
        return updated

    def restore_status(self, actor_id: str, key: str) -> StepStatus:
        current = self._status(key)
        if current.archived_at is None:
            raise ConflictError("this status is not archived")
        self._label_free(current.label, key)
        updated = self._store.update_status(key, {"archived_at": None})
        self._record(actor_id, "pipeline.status_restored", f"Status {current.label} restored", {"key": key})
        return updated

    # -- step types ---------------------------------------------------------------------------

    def step_types(self, include_archived: bool = False) -> list[StepType]:
        rows = sorted(self._store.step_types(), key=lambda t: t.id)
        return rows if include_archived else [t for t in rows if t.archived_at is None]

    def get_step_type(self, type_id: int) -> StepType:
        found = self._store.get_step_type(type_id)
        if found is None:
            raise NotFoundError("step type not found")
        return found

    def _clean(self, edit: StepTypeEdit) -> StepTypeEdit:
        guide_ids = list(dict.fromkeys(edit.guide_ids))
        if len(guide_ids) > MAX_GUIDES:
            raise ValidationError(f"at most {MAX_GUIDES} default guides")
        missing = set(guide_ids) - self._store.live_guide_ids(guide_ids)
        if missing:
            raise ValidationError(f"guides not found or archived: {sorted(missing)}")
        if edit.form_template_id is not None and not self._store.form_template_ready(edit.form_template_id):
            raise ValidationError("the form template does not exist, is archived or its fields are not checked yet")
        docs = []
        for doc in edit.required_documents:
            doc = _name(doc, MAX_REQUIRED_DOC_CHARS, "required document name") if doc.strip() else ""
            if doc and doc.lower() not in (d.lower() for d in docs):
                docs.append(doc)
        if len(docs) > MAX_REQUIRED_DOCS:
            raise ValidationError(f"at most {MAX_REQUIRED_DOCS} required documents")
        return StepTypeEdit(
            name=_name(edit.name, MAX_NAME_CHARS, "step type name"),
            description=_text(edit.description, MAX_DESCRIPTION_CHARS, "description"),
            instructions=_text(edit.instructions, MAX_INSTRUCTIONS_CHARS, "instructions"),
            guide_ids=guide_ids,
            form_template_id=edit.form_template_id,
            input_earlier_steps=edit.input_earlier_steps,
            input_board_documents=edit.input_board_documents,
            required_documents=docs,
        )

    def create_step_type(self, actor_id: str, edit: StepTypeEdit) -> StepType:
        edit = self._clean(edit)
        try:
            created = self._store.create_step_type(edit, actor_id)
        except NameTakenError as exc:
            raise ConflictError("a step type with this name already exists") from exc
        self._record(
            actor_id, "pipeline.step_type_created", f"Step type {edit.name} created",
            {"step_type_id": created.id, "name": edit.name},
        )
        return created

    def update_step_type(self, actor_id: str, type_id: int, edit: StepTypeEdit) -> StepType:
        current = self.get_step_type(type_id)
        if current.archived_at is not None:
            raise ConflictError("this step type is archived; restore it first")
        edit = self._clean(edit)
        values = {
            "name": edit.name,
            "description": edit.description,
            "instructions": edit.instructions,
            "guide_ids": edit.guide_ids,
            "form_template_id": edit.form_template_id,
            "input_earlier_steps": edit.input_earlier_steps,
            "input_board_documents": edit.input_board_documents,
            "required_documents": edit.required_documents,
            "updated_at": self._clock().isoformat(),
        }
        try:
            updated = self._store.update_step_type(type_id, values, actor_id)
        except NameTakenError as exc:
            raise ConflictError("a step type with this name already exists") from exc
        self._record(
            actor_id, "pipeline.step_type_updated", f"Step type {edit.name} changed",
            {
                "step_type_id": type_id,
                "name": {"from": current.name, "to": edit.name},
                "instructions_chars": {"from": len(current.instructions), "to": len(edit.instructions)},
            },
        )
        return updated

    def archive_step_type(self, actor_id: str, type_id: int) -> StepType:
        current = self.get_step_type(type_id)
        if current.archived_at is not None:
            raise ConflictError("this step type is already archived")
        used = [t for t in self._store.templates() if type_id in t.step_type_ids]
        if used:
            where = ", ".join(
                "global" if t.hospital_id is None else f"hospital #{t.hospital_id}" for t in used
            )
            raise ConflictError(f"remove it from the default step order first ({where})")
        updated = self._store.update_step_type(
            type_id, {"archived_at": self._clock().isoformat()}, actor_id
        )
        self._record(
            actor_id, "pipeline.step_type_archived", f"Step type {current.name} archived",
            {"step_type_id": type_id},
        )
        return updated

    def restore_step_type(self, actor_id: str, type_id: int) -> StepType:
        current = self.get_step_type(type_id)
        if current.archived_at is None:
            raise ConflictError("this step type is not archived")
        try:
            updated = self._store.update_step_type(type_id, {"archived_at": None}, actor_id)
        except NameTakenError as exc:
            raise ConflictError("a live step type has the same name; rename one of them first") from exc
        self._record(
            actor_id, "pipeline.step_type_restored", f"Step type {current.name} restored",
            {"step_type_id": type_id},
        )
        return updated

    # -- default step order -------------------------------------------------------------------

    def templates(self) -> list[PipelineTemplate]:
        """The global list first, then per-hospital lists by hospital id."""
        return sorted(
            self._store.templates(), key=lambda t: (t.hospital_id is not None, t.hospital_id or 0)
        )

    def template_for(self, hospital_id: int) -> PipelineTemplate:
        """The list a new pipeline at this hospital starts from (phase 2)."""
        rows = self._store.templates()
        own = next((t for t in rows if t.hospital_id == hospital_id), None)
        found = own or next((t for t in rows if t.hospital_id is None), None)
        if found is None:
            raise ConflictError("no default step order is set; ask an admin")
        return found

    def save_template(
        self, actor_id: str, hospital_id: int | None, step_type_ids: list[int]
    ) -> PipelineTemplate:
        if not 1 <= len(step_type_ids) <= MAX_TEMPLATE_STEPS:
            raise ValidationError(f"the step order needs 1 to {MAX_TEMPLATE_STEPS} steps")
        if len(set(step_type_ids)) != len(step_type_ids):
            raise ValidationError("a step type appears twice")
        live = {t.id for t in self.step_types()}
        unknown = [i for i in step_type_ids if i not in live]
        if unknown:
            raise ValidationError(f"step types not found or archived: {unknown}")
        if hospital_id is not None and not self._store.hospital_exists(hospital_id):
            raise NotFoundError("hospital not found")
        saved = self._store.save_template(hospital_id, step_type_ids, actor_id)
        self._record(
            actor_id, "pipeline.template_saved",
            "Default step order saved" if hospital_id is None else f"Step order for hospital #{hospital_id} saved",
            {"hospital_id": hospital_id, "step_type_ids": step_type_ids},
        )
        return saved

    def delete_template(self, actor_id: str, hospital_id: int) -> None:
        if not self._store.delete_template(hospital_id):
            raise NotFoundError("this hospital has no step order of its own")
        self._record(
            actor_id, "pipeline.template_deleted", f"Step order for hospital #{hospital_id} removed",
            {"hospital_id": hospital_id},
        )
