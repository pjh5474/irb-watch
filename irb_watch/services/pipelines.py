"""IRB pipeline, phase 2: users' pipelines, steps, step documents and history.

Rules (owner, 2026-11-11; see docs/decisions.md "IRB pipeline: user pipelines"):
  * One pipeline per project and hospital, started by the project's owner with a button. It
    copies the hospital's step order (or the global one); archived step types are skipped. New
    steps start in the first live open status. Owner only: anyone else gets "not found".
  * Starting needs the hospital in the project and readable by the user. Afterwards the
    pipeline stays usable even if the hospital leaves the project or the user loses access to
    it (it is the user's own data); only the board documents become unavailable (phase 3).
  * Steps: add from a live step type (its name by default) or free (a name only), rename,
    reorder, remove softly and restore. Status must be a live status. Dates: setting the status
    `submitted` fills the submitted date with today and a closed status fills the decision date,
    when empty and not sent in the same change; the user can change or clear them.
  * Documents: one file per upload; the name is a checklist name or typed. The same name (any
    case) on the same step adds a version; an upload identical to the latest version is refused.
    Same per-file limit as scans. Text is read for PDF, HWP, HWPX and DOCX; a file whose bytes
    are not what its name says is refused. Single versions are never deleted; a whole document
    is removed softly.
  * Removed pipelines, steps and documents are purged with their files after 30 days.
  * Every change is written to pipeline_events (the page's history).
"""

import hashlib
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from irb_watch.parsers.errors import ParseError
from irb_watch.parsers.formats import extract_text
from irb_watch.services.errors import (
    ConflictError,
    NotFoundError,
    UpstreamError,
    ValidationError,
)
from irb_watch.services.pipeline_admin import PipelineSetupService, StepStatus, StepType
from irb_watch.services.users import UserStore, active_profile

log = logging.getLogger(__name__)

PURGE_DAYS = 30
MAX_STEP_NAME = 100
MAX_DOC_NAME = 200
MAX_NOTE = 4000
MAX_STEPS = 60
MAX_TEXT_CHARS = 2_000_000
SUBMITTED = "submitted"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Pipeline:
    id: int
    user_id: str
    project_id: int
    source_id: int
    created_at: str
    updated_at: str
    deleted_at: str | None = None


@dataclass(frozen=True)
class Step:
    id: int
    pipeline_id: int
    step_type_id: int | None
    name: str
    position: int
    status_key: str
    due_on: str | None = None
    submitted_on: str | None = None
    decided_on: str | None = None
    note: str = ""
    deleted_at: str | None = None
    updated_at: str = ""


@dataclass(frozen=True)
class DocVersion:
    id: int
    document_id: int
    version_no: int
    filename: str
    storage_path: str
    sha256: str
    size_bytes: int
    has_text: bool
    text_note: str | None
    created_at: str


@dataclass(frozen=True)
class StepDocument:
    id: int
    step_id: int
    name: str
    created_at: str
    deleted_at: str | None = None
    versions: tuple[DocVersion, ...] = ()  # newest first


@dataclass(frozen=True)
class NewVersion:
    filename: str
    storage_path: str
    sha256: str
    size_bytes: int
    body_text: str | None
    text_note: str | None
    uploaded_by: str


@dataclass(frozen=True)
class Event:
    id: int
    step_id: int | None
    actor_id: str | None
    event_type: str
    payload: dict
    created_at: str


@dataclass(frozen=True)
class ChecklistItem:
    name: str
    uploaded: bool


@dataclass(frozen=True)
class StepView:
    step: Step
    step_type_name: str | None
    documents: list[StepDocument]
    checklist: list[ChecklistItem]


@dataclass(frozen=True)
class PipelineSummary:
    pipeline: Pipeline
    source_name: str | None  # None: the user can no longer read the hospital
    in_project: bool
    steps: int
    closed_steps: int


@dataclass(frozen=True)
class PipelineDetail:
    summary: PipelineSummary
    steps: list[StepView]
    statuses: list[StepStatus]
    step_types: list[StepType] = field(default_factory=list)


class PipelineStore(Protocol):
    # projects and sources
    def project_owner(self, project_id: int) -> str | None: ...
    def project_source_ids(self, project_id: int) -> set[int]: ...
    def can_read_source(self, user_id: str, source_id: int, is_admin: bool) -> bool: ...
    def source_names(self, source_ids: list[int], user_id: str, is_admin: bool) -> dict[int, str]: ...
    # pipelines
    def pipelines_of_project(self, project_id: int) -> list[Pipeline]: ...
    def get_pipeline(self, pipeline_id: int) -> Pipeline | None: ...
    def create_pipeline(self, user_id: str, project_id: int, source_id: int, steps: list[dict]) -> Pipeline: ...
    def set_pipeline_deleted(self, pipeline_id: int, deleted_at: str | None) -> Pipeline: ...
    def touch_pipeline(self, pipeline_id: int) -> None: ...
    # steps
    def steps(self, pipeline_ids: list[int]) -> list[Step]: ...
    def add_step(self, pipeline_id: int, values: dict) -> Step: ...
    def update_step(self, step_id: int, values: dict) -> Step: ...
    def reorder_steps(self, pipeline_id: int, step_ids: list[int]) -> None:
        """All or nothing; raises StepListError if the list is not exactly the live steps."""
        ...
    # documents
    def documents(self, step_ids: list[int]) -> list[StepDocument]: ...
    def create_document(self, step_id: int, name: str) -> StepDocument: ...
    def delete_empty_document(self, document_id: int) -> None: ...
    def set_document_deleted(self, document_id: int, deleted_at: str | None) -> StepDocument: ...
    def add_version(self, document_id: int, version: NewVersion) -> DocVersion:
        """Raises NameTakenError if another upload took the same version number."""
        ...
    # history
    def add_event(self, pipeline_id: int, step_id: int | None, actor_id: str, event_type: str, payload: dict) -> None: ...
    def events(self, pipeline_id: int, limit: int) -> list[Event]: ...
    # purge, one kind ("pipelines" | "steps" | "documents") at a time
    def purge_candidates(self, kind: str, cutoff: str) -> tuple[list[int], list[str]]:
        """Ids removed before `cutoff` and the file paths of every version under them."""
        ...
    def purge_rows(self, kind: str, ids: list[int]) -> None: ...


class PipelineFiles(Protocol):
    def put(self, path: str, content: bytes) -> None: ...
    def delete(self, paths: list[str]) -> None: ...
    def sign(self, path: str, filename: str, expires_in: int) -> str: ...


class NameTakenError(Exception):
    """Raised by the store when a live document or pipeline with the same key exists."""


class StepListError(Exception):
    """Raised by the store when a new step order is not exactly the pipeline's live steps."""


PURGE_KINDS = ("pipelines", "steps", "documents")


def _clean(value: str, limit: int, what: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValidationError(f"the {what} is empty")
    if len(value) > limit:
        raise ValidationError(f"the {what} may have at most {limit} characters")
    return value


def _date(value: str | None, what: str) -> str | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValidationError(f"the {what} must look like 2026-11-30") from exc


def safe_filename(filename: str) -> str:
    """The file name for storage paths: the base name only, unsafe characters replaced."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    stem = _UNSAFE.sub("_", stem).strip("._") or "file"
    ext = _UNSAFE.sub("", ext)[:10]
    return f"{stem[:80]}.{ext}" if ext else stem[:80]


class PipelineService:
    def __init__(
        self,
        users: UserStore,
        store: PipelineStore,
        setup: PipelineSetupService,
        files: PipelineFiles,
        *,
        max_file_bytes: int = 20_000_000,
        link_ttl_seconds: int = 60,
        timezone: str = "Asia/Seoul",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._users = users
        self._store = store
        self._setup = setup
        self._files = files
        self._max_bytes = max_file_bytes
        self._ttl = link_ttl_seconds
        self._tz = ZoneInfo(timezone)
        self._clock = clock

    # -- access -------------------------------------------------------------------------------

    def _user(self, user_id: str) -> bool:
        """The user's account is active; returns whether they are a platform admin."""
        return active_profile(self._users, user_id, "your account is not active").role == "admin"

    def _own_project(self, user_id: str, project_id: int) -> None:
        if self._store.project_owner(project_id) != user_id:
            raise NotFoundError("project not found")

    def _pipeline(self, user_id: str, pipeline_id: int, *, live: bool = True) -> Pipeline:
        self._user(user_id)
        pipeline = self._store.get_pipeline(pipeline_id)
        if pipeline is None or pipeline.user_id != user_id:
            raise NotFoundError("pipeline not found")
        if live and pipeline.deleted_at is not None:
            raise ConflictError("this pipeline is removed; restore it first")
        return pipeline

    def _step(self, pipeline: Pipeline, step_id: int, *, live: bool = True) -> Step:
        step = next((s for s in self._store.steps([pipeline.id]) if s.id == step_id), None)
        if step is None:
            raise NotFoundError("step not found")
        if live and step.deleted_at is not None:
            raise ConflictError("this step is removed; restore it first")
        return step

    def _now(self) -> str:
        return self._clock().isoformat()

    def _today(self) -> str:
        return self._clock().astimezone(self._tz).date().isoformat()

    def _event(self, pipeline_id: int, step_id: int | None, actor: str, kind: str, payload: dict) -> None:
        self._store.add_event(pipeline_id, step_id, actor, kind, payload)
        self._store.touch_pipeline(pipeline_id)

    def _update_step(self, step_id: int, values: dict) -> Step:
        try:
            return self._store.update_step(step_id, values)
        except LookupError as exc:
            raise NotFoundError("this step no longer exists; reload the page") from exc

    def _first_open_status(self) -> str:
        status = next((s for s in self._setup.statuses() if not s.closed), None)
        if status is None:
            raise ConflictError("no open step status is set; ask an admin")
        return status.key

    # -- pipelines ----------------------------------------------------------------------------

    def _summaries(self, user_id: str, is_admin: bool, project_id: int, pipelines: list[Pipeline]) -> list[PipelineSummary]:
        in_project = self._store.project_source_ids(project_id)
        names = self._store.source_names([p.source_id for p in pipelines], user_id, is_admin)
        closed = {s.key for s in self._setup.statuses(include_archived=True) if s.closed}
        steps = [s for s in self._store.steps([p.id for p in pipelines]) if s.deleted_at is None]
        out = []
        for p in pipelines:
            own = [s for s in steps if s.pipeline_id == p.id]
            out.append(
                PipelineSummary(
                    p, names.get(p.source_id), p.source_id in in_project, len(own),
                    sum(1 for s in own if s.status_key in closed),
                )
            )
        return out

    def list_for_project(self, user_id: str, project_id: int, include_removed: bool = False) -> list[PipelineSummary]:
        is_admin = self._user(user_id)
        self._own_project(user_id, project_id)
        pipelines = [
            p for p in self._store.pipelines_of_project(project_id)
            if p.user_id == user_id and (include_removed or p.deleted_at is None)
        ]
        return self._summaries(user_id, is_admin, project_id, pipelines)

    def start(self, user_id: str, project_id: int, source_id: int) -> Pipeline:
        is_admin = self._user(user_id)
        self._own_project(user_id, project_id)
        if source_id not in self._store.project_source_ids(project_id):
            raise ValidationError("this hospital is not in the project; add it first")
        if not self._store.can_read_source(user_id, source_id, is_admin):
            raise NotFoundError("source not found")
        if any(
            p.source_id == source_id and p.deleted_at is None
            for p in self._store.pipelines_of_project(project_id)
        ):
            raise ConflictError("this hospital already has a pipeline in the project")
        order = self._setup.template_for(source_id)
        live = {t.id: t for t in self._setup.step_types()}
        status = self._first_open_status()
        steps = [
            {"step_type_id": tid, "name": live[tid].name, "position": i, "status_key": status}
            for i, tid in enumerate((t for t in order.step_type_ids if t in live), start=1)
        ]
        try:
            pipeline = self._store.create_pipeline(user_id, project_id, source_id, steps)
        except NameTakenError as exc:
            raise ConflictError("this hospital already has a pipeline in the project") from exc
        self._event(
            pipeline.id, None, user_id, "pipeline.started",
            {"source_id": source_id, "steps": [s["name"] for s in steps], "from": "hospital" if order.source_id else "global"},
        )
        return pipeline

    def detail(self, user_id: str, pipeline_id: int) -> PipelineDetail:
        is_admin = self._user(user_id)
        pipeline = self._pipeline(user_id, pipeline_id, live=False)
        summary = self._summaries(user_id, is_admin, pipeline.project_id, [pipeline])[0]
        steps = sorted(self._store.steps([pipeline.id]), key=lambda s: (s.deleted_at is not None, s.position, s.id))
        docs = self._store.documents([s.id for s in steps])
        all_types = {t.id: t for t in self._setup.step_types(include_archived=True)}
        views = []
        for step in steps:
            own = [d for d in docs if d.step_id == step.id]
            live_names = {d.name.lower() for d in own if d.deleted_at is None and d.versions}
            stype = all_types.get(step.step_type_id) if step.step_type_id else None
            required = list(stype.required_documents) if stype else []
            views.append(
                StepView(
                    step,
                    stype.name if stype else None,
                    sorted(own, key=lambda d: (d.deleted_at is not None, d.name.lower())),
                    [ChecklistItem(r, r.lower() in live_names) for r in required],
                )
            )
        return PipelineDetail(
            summary, views, self._setup.statuses(include_archived=True), self._setup.step_types()
        )

    def remove(self, user_id: str, pipeline_id: int) -> Pipeline:
        self._pipeline(user_id, pipeline_id)
        removed = self._store.set_pipeline_deleted(pipeline_id, self._now())
        self._event(pipeline_id, None, user_id, "pipeline.removed", {})
        return removed

    def restore(self, user_id: str, pipeline_id: int) -> Pipeline:
        pipeline = self._pipeline(user_id, pipeline_id, live=False)
        if pipeline.deleted_at is None:
            raise ConflictError("this pipeline is not removed")
        try:
            restored = self._store.set_pipeline_deleted(pipeline_id, None)
        except NameTakenError as exc:
            raise ConflictError(
                "this hospital has a newer pipeline in the project; remove that one first"
            ) from exc
        self._event(pipeline_id, None, user_id, "pipeline.restored", {})
        return restored

    # -- steps --------------------------------------------------------------------------------

    def add_step(self, user_id: str, pipeline_id: int, name: str | None, step_type_id: int | None) -> Step:
        pipeline = self._pipeline(user_id, pipeline_id)
        steps = self._store.steps([pipeline.id])
        if sum(1 for s in steps if s.deleted_at is None) >= MAX_STEPS:
            raise ConflictError(f"a pipeline may have at most {MAX_STEPS} steps")
        if step_type_id is not None:
            stype = next((t for t in self._setup.step_types() if t.id == step_type_id), None)
            if stype is None:
                raise ValidationError("step type not found or archived")
            name = _clean(name or stype.name, MAX_STEP_NAME, "step name")
        else:
            name = _clean(name or "", MAX_STEP_NAME, "step name")
        position = max((s.position for s in steps), default=0) + 1
        step = self._store.add_step(
            pipeline.id,
            {"step_type_id": step_type_id, "name": name, "position": position, "status_key": self._first_open_status()},
        )
        self._event(pipeline.id, step.id, user_id, "step.added", {"name": name, "step_type_id": step_type_id})
        return step

    def update_step(self, user_id: str, pipeline_id: int, step_id: int, changes: dict) -> Step:
        """`changes` holds only the fields sent: name, status_key, due_on, submitted_on,
        decided_on, note (a date of None or "" clears it)."""
        pipeline = self._pipeline(user_id, pipeline_id)
        step = self._step(pipeline, step_id)
        values: dict = {}
        if "name" in changes:
            values["name"] = _clean(changes["name"] or "", MAX_STEP_NAME, "step name")
        if "note" in changes:
            note = (changes["note"] or "").strip()
            if len(note) > MAX_NOTE:
                raise ValidationError(f"the note may have at most {MAX_NOTE:,} characters")
            values["note"] = note
        for key, what in (("due_on", "due date"), ("submitted_on", "submitted date"), ("decided_on", "decision date")):
            if key in changes:
                values[key] = _date(changes[key], what)
        if "status_key" in changes and changes["status_key"] != step.status_key:
            status = next((s for s in self._setup.statuses() if s.key == changes["status_key"]), None)
            if status is None:
                raise ValidationError("unknown or archived status")
            values["status_key"] = status.key
            if status.key == SUBMITTED and "submitted_on" not in changes and not step.submitted_on:
                values["submitted_on"] = self._today()
            if status.closed and "decided_on" not in changes and not step.decided_on:
                values["decided_on"] = self._today()
        values = {k: v for k, v in values.items() if getattr(step, k) != v}
        if not values:
            return step
        updated = self._update_step(step.id, {**values, "updated_at": self._now()})
        payload = {k: {"from": getattr(step, k), "to": v} for k, v in values.items() if k != "note"}
        if "note" in values:
            payload["note"] = True
        self._event(pipeline.id, step.id, user_id, "step.updated", payload)
        return updated

    def reorder_steps(self, user_id: str, pipeline_id: int, step_ids: list[int]) -> list[Step]:
        pipeline = self._pipeline(user_id, pipeline_id)
        live = [s for s in self._store.steps([pipeline.id]) if s.deleted_at is None]
        if sorted(step_ids) != sorted(s.id for s in live) or len(set(step_ids)) != len(step_ids):
            raise ValidationError("send every step of the pipeline exactly once")
        try:
            # One database call: the whole order is saved or nothing is.
            self._store.reorder_steps(pipeline.id, step_ids)
        except StepListError as exc:
            raise ConflictError("the steps changed meanwhile; reload the page and try again") from exc
        self._event(pipeline.id, None, user_id, "step.reordered", {"step_ids": step_ids})
        return sorted(
            (s for s in self._store.steps([pipeline.id]) if s.deleted_at is None), key=lambda s: s.position
        )

    def remove_step(self, user_id: str, pipeline_id: int, step_id: int) -> Step:
        pipeline = self._pipeline(user_id, pipeline_id)
        step = self._step(pipeline, step_id)
        removed = self._update_step(step.id, {"deleted_at": self._now()})
        self._event(pipeline.id, step.id, user_id, "step.removed", {"name": step.name})
        return removed

    def restore_step(self, user_id: str, pipeline_id: int, step_id: int) -> Step:
        pipeline = self._pipeline(user_id, pipeline_id)
        step = self._step(pipeline, step_id, live=False)
        if step.deleted_at is None:
            raise ConflictError("this step is not removed")
        live = [s for s in self._store.steps([pipeline.id]) if s.deleted_at is None]
        if len(live) >= MAX_STEPS:
            raise ConflictError(f"a pipeline may have at most {MAX_STEPS} steps; remove one first")
        position = max((s.position for s in live), default=0) + 1
        restored = self._update_step(step.id, {"deleted_at": None, "position": position})
        self._event(pipeline.id, step.id, user_id, "step.restored", {"name": step.name})
        return restored

    # -- documents ----------------------------------------------------------------------------

    def _document(self, step: Step, document_id: int, *, live: bool = True) -> StepDocument:
        doc = next((d for d in self._store.documents([step.id]) if d.id == document_id), None)
        if doc is None:
            raise NotFoundError("document not found")
        if live and doc.deleted_at is not None:
            raise ConflictError("this document is removed; restore it first")
        return doc

    def upload(
        self, user_id: str, pipeline_id: int, step_id: int, name: str, filename: str, content: bytes
    ) -> tuple[StepDocument, DocVersion]:
        pipeline = self._pipeline(user_id, pipeline_id)
        step = self._step(pipeline, step_id)
        name = _clean(name, MAX_DOC_NAME, "document name")
        filename = filename.strip().replace("\\", "/").rsplit("/", 1)[-1]
        if not filename or len(filename) > 255:
            raise ValidationError("the file needs a name of at most 255 characters")
        if not content:
            raise ValidationError("the file is empty")
        if len(content) > self._max_bytes:
            raise ValidationError(f"the file is larger than {self._max_bytes // 1_000_000} MB")
        sha = hashlib.sha256(content).hexdigest()
        docs = self._store.documents([step.id])
        doc = next((d for d in docs if d.deleted_at is None and d.name.lower() == name.lower()), None)
        if doc is not None and doc.versions and doc.versions[0].sha256 == sha:
            raise ConflictError(f"this file is the same as version {doc.versions[0].version_no} of {doc.name}")
        try:
            result = extract_text(filename, content)
        except ParseError as exc:
            raise ValidationError(f"{filename} is not the kind of file its name says") from exc
        text = result.document.text[:MAX_TEXT_CHARS] if result.document else None
        path = f"p{pipeline.id}/s{step.id}/{secrets.token_hex(8)}/{safe_filename(filename)}"
        try:
            self._files.put(path, content)
        except Exception as exc:
            raise UpstreamError("the file could not be stored; nothing was saved") from exc
        created = None  # a document made by this upload, removed again if the upload fails
        try:
            if doc is None:
                try:
                    doc = created = self._store.create_document(step.id, name)
                except NameTakenError:
                    doc = next(
                        d for d in self._store.documents([step.id])
                        if d.deleted_at is None and d.name.lower() == name.lower()
                    )
            version = self._store.add_version(
                doc.id,
                NewVersion(filename, path, sha, len(content), text, result.note if text is None else None, user_id),
            )
        except Exception as exc:
            self._drop([path])
            if created is not None:
                try:
                    self._store.delete_empty_document(created.id)
                except Exception:
                    log.exception("could not remove the empty document %s", created.id)
            if isinstance(exc, NameTakenError):
                raise ConflictError(
                    "another upload of this document was saved at the same moment; reload the page and try again"
                ) from exc
            raise
        self._event(
            pipeline.id, step.id, user_id, "document.uploaded",
            {"document_id": doc.id, "name": doc.name, "version_no": version.version_no, "filename": filename},
        )
        # Read again so the answer includes the version just added.
        fresh = next((d for d in self._store.documents([step.id]) if d.id == doc.id), doc)
        return fresh, version

    def _drop(self, paths: list[str]) -> None:
        try:
            self._files.delete(paths)
        except Exception:
            log.exception("could not delete pipeline files %s", paths)

    def remove_document(self, user_id: str, pipeline_id: int, step_id: int, document_id: int) -> StepDocument:
        pipeline = self._pipeline(user_id, pipeline_id)
        step = self._step(pipeline, step_id)
        doc = self._document(step, document_id)
        removed = self._store.set_document_deleted(doc.id, self._now())
        self._event(pipeline.id, step.id, user_id, "document.removed", {"document_id": doc.id, "name": doc.name})
        return removed

    def restore_document(self, user_id: str, pipeline_id: int, step_id: int, document_id: int) -> StepDocument:
        pipeline = self._pipeline(user_id, pipeline_id)
        step = self._step(pipeline, step_id)
        doc = self._document(step, document_id, live=False)
        if doc.deleted_at is None:
            raise ConflictError("this document is not removed")
        try:
            restored = self._store.set_document_deleted(doc.id, None)
        except NameTakenError as exc:
            raise ConflictError("a document with this name exists in the step; rename or remove it first") from exc
        self._event(pipeline.id, step.id, user_id, "document.restored", {"document_id": doc.id, "name": doc.name})
        return restored

    def download_url(self, user_id: str, pipeline_id: int, step_id: int, document_id: int, version_no: int) -> str:
        pipeline = self._pipeline(user_id, pipeline_id, live=False)
        step = self._step(pipeline, step_id, live=False)
        doc = self._document(step, document_id, live=False)
        version = next((v for v in doc.versions if v.version_no == version_no), None)
        if version is None:
            raise NotFoundError("version not found")
        try:
            url = self._files.sign(version.storage_path, version.filename, self._ttl)
        except Exception as exc:
            raise UpstreamError("could not make a download link; try again") from exc
        self._store.add_event(
            pipeline.id, step.id, user_id, "document.downloaded",
            {"document_id": doc.id, "version_no": version_no},
        )
        return url

    def history(self, user_id: str, pipeline_id: int, limit: int = 200) -> list[Event]:
        pipeline = self._pipeline(user_id, pipeline_id, live=False)
        return self._store.events(pipeline.id, limit)

    # -- purge --------------------------------------------------------------------------------

    def purge_old(self) -> int:
        """Deletes pipelines, steps and documents removed more than 30 days ago, with their
        files. Never raises (runs from the scheduler).

        Per kind, the files go first and the rows after: if the file delete fails the rows stay
        and the next run tries again; if the row delete fails the files are already gone and
        the next run deletes the rows (removing a missing file is harmless). So a file is never
        left without a row pointing at it."""
        cutoff = (self._clock() - timedelta(days=PURGE_DAYS)).isoformat()
        total = 0
        for kind in PURGE_KINDS:
            try:
                ids, paths = self._store.purge_candidates(kind, cutoff)
                if not ids:
                    continue
                if paths:
                    self._files.delete(paths)
                self._store.purge_rows(kind, ids)
                total += len(ids)
            except Exception:
                log.exception("pipeline purge of %s failed; the next run tries again", kind)
        return total
