"""IRB pipeline, phase 2: users' pipelines, steps, step documents and history.

Rules (owner, 2026-11-11; see docs/decisions.md "IRB pipeline: user pipelines"):
  * One pipeline per project and hospital, started by the project's owner with a button. It
    copies the hospital's step order (or the global one); archived step types are skipped. New
    steps start in the first live open status. Owner only: anyone else gets "not found".
  * A pipeline belongs to a hospital (which groups its boards), not to one board (owner,
    2026-11-12). Starting needs the hospital in the project; any hospital may be used, even one
    with no board the user can read (then only their own uploads). Afterwards the pipeline
    stays usable even if the hospital leaves the project (it is the user's own data). Board
    documents stay gated per board (phase 3).
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

from irb_watch.docgen.types import StepContext, StepFile, StepFileRef
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
MAX_DRAFT_FILES = 15  # the user's own files in one AI draft
MAX_TEXT_CHARS = 2_000_000
SUBMITTED = "submitted"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Pipeline:
    id: int
    user_id: str
    project_id: int
    hospital_id: int
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

    @property
    def documents_with_files(self) -> int:
        """Live documents that hold at least one version (the overview's count)."""
        return sum(1 for d in self.documents if d.deleted_at is None and d.versions)

    @property
    def required_missing(self) -> int:
        return sum(1 for c in self.checklist if not c.uploaded)


@dataclass(frozen=True)
class PipelineSummary:
    pipeline: Pipeline
    hospital_name: str | None  # None: the hospital row is gone (should not happen)
    in_project: bool
    steps: int
    closed_steps: int


@dataclass(frozen=True)
class PipelineDetail:
    summary: PipelineSummary
    steps: list[StepView]
    statuses: list[StepStatus]
    step_types: list[StepType] = field(default_factory=list)


@dataclass(frozen=True)
class StepPage:
    """One step with its documents (phase A: one step per page). previous / next follow the
    overview's order over live steps; a removed step has neither."""

    summary: PipelineSummary
    view: StepView
    previous_step_id: int | None
    next_step_id: int | None
    statuses: list[StepStatus]


@dataclass(frozen=True)
class WorkStep:
    """One live step of a live pipeline, with what the work page and the reminders show
    (phase A2). required / required_missing are 0 where documents were not read (reminders)."""

    pipeline_id: int
    project_id: int
    project_name: str | None  # None: the project row is gone (should not happen)
    hospital_id: int
    hospital_name: str | None
    step_id: int
    step_name: str
    status_key: str
    status_label: str
    closed: bool
    due_on: str | None
    submitted_on: str | None
    decided_on: str | None
    required: int = 0
    required_missing: int = 0


class PipelineStore(Protocol):
    # projects and hospitals
    def project_owner(self, project_id: int) -> str | None: ...
    def project_hospital_ids(self, project_id: int) -> set[int]: ...
    def hospital_names(self, hospital_ids: list[int]) -> dict[int, str]: ...
    def project_names(self, project_ids: list[int]) -> dict[int, str]: ...
    # pipelines
    def pipelines_of_project(self, project_id: int) -> list[Pipeline]: ...
    def live_pipelines_of_user(self, user_id: str) -> list[Pipeline]: ...
    def live_pipelines(self, pipeline_ids: list[int]) -> list[Pipeline]: ...
    def due_steps(self, until: str, skip_statuses: list[str]) -> list[Step]:
        """Live steps with a due date on or before `until` whose status is not in skip_statuses
        (any pipeline, removed ones too; the caller keeps those of live pipelines)."""
        ...
    def get_pipeline(self, pipeline_id: int) -> Pipeline | None: ...
    def create_pipeline(self, user_id: str, project_id: int, hospital_id: int, steps: list[dict]) -> Pipeline: ...
    def set_pipeline_deleted(self, pipeline_id: int, deleted_at: str | None) -> Pipeline: ...
    def touch_pipeline(self, pipeline_id: int) -> None: ...
    # steps
    def steps(self, pipeline_ids: list[int]) -> list[Step]: ...
    def get_step(self, step_id: int) -> Step | None: ...
    def version_text(self, version_id: int) -> str | None:
        """The text read from one uploaded version (None: no text was read)."""
        ...

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
    def events(self, pipeline_id: int, limit: int, step_id: int | None = None) -> list[Event]:
        """Newest first; with step_id, only that step's events."""
        ...
    # purge, one kind ("pipelines" | "steps" | "documents") at a time
    def purge_candidates(self, kind: str, cutoff: str) -> tuple[list[int], list[str]]:
        """Ids removed before `cutoff` and the file paths of every version under them."""
        ...
    def purge_rows(self, kind: str, ids: list[int]) -> None: ...


@dataclass(frozen=True)
class DraftFileChoice:
    """One of the user's own files offered for an AI draft of a step (its newest version)."""

    document_id: int
    name: str
    step_id: int
    step_name: str
    version_no: int
    filename: str
    has_text: bool  # False: stored only; it cannot be sent to the AI
    text_note: str | None
    ticked: bool  # sent unless the user unticks it


@dataclass(frozen=True)
class DraftInputs:
    """What the "Draft for this step" dialog starts from. Nothing here is sent anywhere."""

    step_id: int
    step_name: str
    hospital_id: int
    files: list[DraftFileChoice]
    allow_board_documents: bool
    guide_ids: tuple[int, ...]  # the step type's default guides
    form_template_id: int | None  # the step type's default form
    has_instructions: bool  # the step type has added AI instructions


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
        return self.today().isoformat()

    def today(self) -> date:
        """Today in the configured time zone (the one step dates are filled in)."""
        return self._clock().astimezone(self._tz).date()

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

    def _summaries(self, project_id: int, pipelines: list[Pipeline]) -> list[PipelineSummary]:
        in_project = self._store.project_hospital_ids(project_id)
        names = self._store.hospital_names([p.hospital_id for p in pipelines])
        closed = {s.key for s in self._setup.statuses(include_archived=True) if s.closed}
        steps = [s for s in self._store.steps([p.id for p in pipelines]) if s.deleted_at is None]
        out = []
        for p in pipelines:
            own = [s for s in steps if s.pipeline_id == p.id]
            out.append(
                PipelineSummary(
                    p, names.get(p.hospital_id), p.hospital_id in in_project, len(own),
                    sum(1 for s in own if s.status_key in closed),
                )
            )
        return out

    def list_for_project(self, user_id: str, project_id: int, include_removed: bool = False) -> list[PipelineSummary]:
        self._user(user_id)
        self._own_project(user_id, project_id)
        pipelines = [
            p for p in self._store.pipelines_of_project(project_id)
            if p.user_id == user_id and (include_removed or p.deleted_at is None)
        ]
        return self._summaries(project_id, pipelines)

    def start(self, user_id: str, project_id: int, hospital_id: int) -> Pipeline:
        self._user(user_id)
        self._own_project(user_id, project_id)
        if hospital_id not in self._store.project_hospital_ids(project_id):
            raise ValidationError("this hospital is not in the project; add it first")
        if any(
            p.hospital_id == hospital_id and p.deleted_at is None
            for p in self._store.pipelines_of_project(project_id)
        ):
            raise ConflictError("this hospital already has a pipeline in the project")
        order = self._setup.template_for(hospital_id)
        live = {t.id: t for t in self._setup.step_types()}
        status = self._first_open_status()
        steps = [
            {"step_type_id": tid, "name": live[tid].name, "position": i, "status_key": status}
            for i, tid in enumerate((t for t in order.step_type_ids if t in live), start=1)
        ]
        try:
            pipeline = self._store.create_pipeline(user_id, project_id, hospital_id, steps)
        except NameTakenError as exc:
            raise ConflictError("this hospital already has a pipeline in the project") from exc
        self._event(
            pipeline.id, None, user_id, "pipeline.started",
            {
                "hospital_id": hospital_id, "steps": [s["name"] for s in steps],
                "from": "hospital" if order.hospital_id else "global",
            },
        )
        return pipeline

    def _ordered_steps(self, pipeline: Pipeline) -> list[Step]:
        """The overview's order: live steps by position, then removed ones."""
        return sorted(self._store.steps([pipeline.id]), key=lambda s: (s.deleted_at is not None, s.position, s.id))

    def _views(self, steps: list[Step]) -> list[StepView]:
        by_step: dict[int, list[StepDocument]] = {}
        for d in self._store.documents([s.id for s in steps]):  # grouped once, not per step
            by_step.setdefault(d.step_id, []).append(d)
        all_types = {t.id: t for t in self._setup.step_types(include_archived=True)}
        views = []
        for step in steps:
            own = by_step.get(step.id, [])
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
        return views

    def detail(self, user_id: str, pipeline_id: int) -> PipelineDetail:
        pipeline = self._pipeline(user_id, pipeline_id, live=False)
        summary = self._summaries(pipeline.project_id, [pipeline])[0]
        views = self._views(self._ordered_steps(pipeline))
        return PipelineDetail(
            summary, views, self._setup.statuses(include_archived=True), self._setup.step_types()
        )

    def step_page(self, user_id: str, pipeline_id: int, step_id: int) -> StepPage:
        """One step for its own page. A removed step or a step of a removed pipeline still
        opens (read-only in the page; files stay downloadable); a stranger gets not found."""
        pipeline = self._pipeline(user_id, pipeline_id, live=False)
        steps = self._ordered_steps(pipeline)
        step = next((s for s in steps if s.id == step_id), None)
        if step is None:
            raise NotFoundError("step not found")
        previous_id = next_id = None
        if step.deleted_at is None:
            live = [s.id for s in steps if s.deleted_at is None]
            at = live.index(step.id)
            previous_id = live[at - 1] if at > 0 else None
            next_id = live[at + 1] if at + 1 < len(live) else None
        return StepPage(
            self._summaries(pipeline.project_id, [pipeline])[0],
            self._views([step])[0],
            previous_id,
            next_id,
            self._setup.statuses(include_archived=True),
        )

    # -- work across projects (phase A2) ------------------------------------------------------

    def _work_steps(self, pipelines: list[Pipeline], steps: list[Step], views: dict[int, StepView]) -> list[WorkStep]:
        by_id = {p.id: p for p in pipelines}
        hospitals = self._store.hospital_names(sorted({p.hospital_id for p in pipelines}))
        projects = self._store.project_names(sorted({p.project_id for p in pipelines}))
        statuses = {s.key: s for s in self._setup.statuses(include_archived=True)}
        out = []
        for s in steps:
            p = by_id[s.pipeline_id]
            status = statuses.get(s.status_key)
            view = views.get(s.id)
            out.append(
                WorkStep(
                    p.id, p.project_id, projects.get(p.project_id), p.hospital_id, hospitals.get(p.hospital_id),
                    s.id, s.name, s.status_key, status.label if status else s.status_key,
                    bool(status and status.closed), s.due_on, s.submitted_on, s.decided_on,
                    len(view.checklist) if view else 0, view.required_missing if view else 0,
                )
            )
        return out

    def work_steps(self, user_id: str) -> list[WorkStep]:
        """Every live step of the user's live pipelines, over all projects (pipelines whose
        hospital left the project too), with its checklist counts. For the work page."""
        self._user(user_id)
        pipelines = self._store.live_pipelines_of_user(user_id)
        if not pipelines:
            return []
        steps = [s for s in self._store.steps([p.id for p in pipelines]) if s.deleted_at is None]
        views = {v.step.id: v for v in self._views(steps)}
        return self._work_steps(pipelines, steps, views)

    def reminder_steps(self, until: str) -> dict[str, list[WorkStep]]:
        """Live steps of live pipelines with a due date on or before `until`, per pipeline owner
        (the reminders' only recipient until projects are shared). Documents are not read. Run
        by the daily digest; the caller decides which steps are due for a reminder."""
        # Finished and submitted steps never get a reminder: leave them out in the query, so the
        # daily read does not grow with the history.
        closed = [s.key for s in self._setup.statuses(include_archived=True) if s.closed]
        steps = self._store.due_steps(until, sorted({*closed, SUBMITTED}))
        if not steps:
            return {}
        pipelines = self._store.live_pipelines(sorted({s.pipeline_id for s in steps}))
        live = {p.id for p in pipelines}
        steps = [s for s in steps if s.pipeline_id in live and s.deleted_at is None]
        owner = {p.id: p.user_id for p in pipelines}
        out: dict[str, list[WorkStep]] = {}
        for w in self._work_steps(pipelines, steps, {}):
            out.setdefault(owner[w.pipeline_id], []).append(w)
        return out

    # -- AI drafts of a step (phase 3) ---------------------------------------------------------
    # The draft itself is the docs generator's (services/docgen.py); this side says which of the
    # user's own files may be used and hands over the step type's AI settings.

    def _draft_scope(self, pipeline: Pipeline, step: Step) -> tuple[StepType | None, list[Step]]:
        """The step's type (archived ones too: its settings still apply to existing steps) and
        the steps whose files may be used: this one, plus the earlier live ones unless the type
        says no. A step without a type (the user's own) may use earlier steps."""
        stype = None
        if step.step_type_id is not None:
            types = {t.id: t for t in self._setup.step_types(include_archived=True)}
            stype = types.get(step.step_type_id)
        allowed = [step]
        if stype is None or stype.input_earlier_steps:
            earlier = [
                s for s in self._store.steps([pipeline.id])
                if s.deleted_at is None and s.id != step.id and s.position < step.position
            ]
            allowed = sorted(earlier, key=lambda s: s.position) + [step]
        return stype, allowed

    def draft_inputs(self, user_id: str, pipeline_id: int, step_id: int) -> DraftInputs:
        """The dialog's starting point: the newest version of every live document of this step
        and of the earlier steps (when the type allows), ticked when it has readable text."""
        pipeline = self._pipeline(user_id, pipeline_id)
        step = self._step(pipeline, step_id)
        stype, allowed = self._draft_scope(pipeline, step)
        names = {s.id: s.name for s in allowed}
        order = {s.id: i for i, s in enumerate(allowed)}
        docs = [
            d for d in self._store.documents(list(names))
            if d.deleted_at is None and d.versions
        ]
        files: list[DraftFileChoice] = []
        ticked = 0
        # This step's files first; only as many are ticked as one draft may send.
        for d in sorted(docs, key=lambda d: (-order[d.step_id], d.name.lower())):
            newest = d.versions[0]
            tick = newest.has_text and ticked < MAX_DRAFT_FILES
            ticked += tick
            files.append(
                DraftFileChoice(
                    d.id, d.name, d.step_id, names[d.step_id], newest.version_no,
                    newest.filename, newest.has_text, newest.text_note, ticked=tick,
                )
            )
        return DraftInputs(
            step.id, step.name, pipeline.hospital_id, files,
            allow_board_documents=stype is None or stype.input_board_documents,
            guide_ids=tuple(stype.guide_ids) if stype else (),
            form_template_id=stype.form_template_id if stype else None,
            has_instructions=bool(stype and stype.instructions.strip()),
        )

    def context(self, user_id: str, step_id: int, files: list[tuple[int, int | None]]) -> StepContext:
        """For the docs generator (docgen.types.StepReader): the step must be the user's own
        and live; each file must be a live document of this step or an allowed earlier one, at
        an existing version with readable text."""
        found = self._store.get_step(step_id)
        if found is None:
            raise NotFoundError("step not found")
        try:
            pipeline = self._pipeline(user_id, found.pipeline_id)
        except NotFoundError as exc:
            raise NotFoundError("step not found") from exc
        step = self._step(pipeline, step_id)
        stype, allowed = self._draft_scope(pipeline, step)
        wanted = list(dict.fromkeys((int(d), v) for d, v in files))
        if len({d for d, _ in wanted}) != len(wanted):
            raise ValidationError("a file is chosen twice")
        if len(wanted) > MAX_DRAFT_FILES:
            raise ValidationError(f"choose at most {MAX_DRAFT_FILES} of your files")
        names = {s.id: s.name for s in allowed}
        docs = {
            d.id: d for d in self._store.documents(list(names)) if d.deleted_at is None
        }
        out: list[StepFile] = []
        for document_id, version_no in wanted:
            doc = docs.get(document_id)
            if doc is None or not doc.versions:
                raise NotFoundError(
                    f"file {document_id} is not a document of this step or of a step before it"
                )
            version = (
                doc.versions[0] if version_no is None
                else next((v for v in doc.versions if v.version_no == version_no), None)
            )
            if version is None:
                raise NotFoundError(f'"{doc.name}" has no version {version_no}')
            text = self._store.version_text(version.id) if version.has_text else None
            if not text or not text.strip():
                reason = version.text_note or "no text could be read from it"
                raise ValidationError(f'"{doc.name}" ({version.filename}) cannot be sent to the AI: {reason}')
            out.append(
                StepFile(
                    StepFileRef(doc.id, version.version_no, doc.name, doc.step_id, names[doc.step_id]),
                    version.filename,
                    text,
                )
            )
        return StepContext(
            step_id=step.id,
            step_name=step.name,
            hospital_id=pipeline.hospital_id,
            files=tuple(out),
            instructions=stype.instructions if stype else "",
            guide_ids=tuple(stype.guide_ids) if stype else (),
            form_template_id=stype.form_template_id if stype else None,
            allow_board_documents=stype is None or stype.input_board_documents,
        )

    def check_own_step(self, user_id: str, step_id: int) -> None:
        """Raises "step not found" unless the step is in the active user's own pipeline
        (before a job row names it)."""
        owner_pipeline = self._own_pipeline_of_step(user_id, step_id)
        self._user(user_id)
        if owner_pipeline is None:
            raise NotFoundError("step not found")

    def _own_pipeline_of_step(self, user_id: str, step_id: int) -> int | None:
        """The one place that says whether a step (found by id alone) is in the user's own
        pipeline: its pipeline id, or None."""
        step = self._store.get_step(step_id)
        pipeline = self._store.get_pipeline(step.pipeline_id) if step is not None else None
        return pipeline.id if pipeline is not None and pipeline.user_id == user_id else None

    def draft_started(self, user_id: str, step_id: int, kind: str, job_id: str) -> None:
        """The page's history shows that an AI draft (or revision) was started in a step.
        Best effort: the job is already running and counted."""
        try:
            step = self._store.get_step(step_id)
            if step is not None:
                self._event(step.pipeline_id, step_id, user_id, f"draft.{kind}_started", {"job_id": job_id})
        except Exception as exc:  # noqa: BLE001
            log.warning("draft start could not be written to the pipeline history: %s", type(exc).__name__)

    def pipeline_of_step(self, user_id: str, step_id: int) -> int | None:
        """The pipeline holding this step, when it is the user's own (removed ones too); None
        otherwise. For the draft page's "Back to step" link only, so it never raises: a failed
        read leaves the link out instead of failing the draft page."""
        try:
            return self._own_pipeline_of_step(user_id, step_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("the pipeline of step %s could not be read: %s", step_id, type(exc).__name__)
            return None

    def step_ids(self, user_id: str, pipeline_id: int) -> list[int]:
        """Every step of the user's pipeline (removed ones too), for listing its drafts."""
        pipeline = self._pipeline(user_id, pipeline_id, live=False)
        return [s.id for s in self._store.steps([pipeline.id])]

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

    def history(self, user_id: str, pipeline_id: int, limit: int = 200, step_id: int | None = None) -> list[Event]:
        pipeline = self._pipeline(user_id, pipeline_id, live=False)
        if step_id is not None:
            self._step(pipeline, step_id, live=False)
        return self._store.events(pipeline.id, limit, step_id)

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
