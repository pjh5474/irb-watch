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

Phase B2b (owner, 2026-11-14; decisions.md "IRB pipeline redesign, phase B2b: steps"):
  * Follow-up steps answer a main step of the same pipeline, one level; they may be added at any
    time. A step with live follow-ups cannot be removed; a follow-up whose main step is removed
    cannot be restored. The overview shows each main step followed by its follow-ups.
  * Numbered step types get the next number when added (editable); `subject` says what the step
    is about. A new pipeline never starts with a follow-up type.
  * The IRB result is a field of its own. Setting it fills the decision date when empty; the
    status is only offered (completed for a final answer, 보완요청 for one that needs an answer).
  * The hospital page lines up a hospital's sites (PIs) with their main steps.
"""

import hashlib
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from irb_watch.docgen.types import StepContext, StepFile, StepFileRef, site_detail_names
from irb_watch.parsers.errors import ParseError
from irb_watch.parsers.formats import extract_text
from irb_watch.services.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UpstreamError,
    ValidationError,
)
from irb_watch.services.parallel import together
from irb_watch.services.pipeline_admin import (
    FOLLOW_UP,
    SECTIONS,
    DocumentKind,
    PipelineSetupService,
    StepResult,
    StepStatus,
    StepType,
)
from irb_watch.services.project_access import NEEDS, REFUSALS
from irb_watch.services.sessions import Session, SessionKind, target_warning
from irb_watch.services.sites import Site, SiteStore, current_facts
from irb_watch.services.users import UserStore, active_profile

log = logging.getLogger(__name__)

PURGE_DAYS = 30
MAX_STEP_NAME = 100
MAX_DOC_NAME = 200
MAX_NOTE = 4000
MAX_STEPS = 60
MAX_DRAFT_FILES = 15  # the user's own files in one AI draft
MAX_TEXT_CHARS = 2_000_000
MAX_SUBJECT = 200
MAX_SEQUENCE = 999
MAX_CUSTOM_DATES = 10
MAX_DATE_LABEL = 60
SUBMITTED = "submitted"
DONE = "done"  # the closed status offered after an approved result (migration step_structure)
ANSWERING = "revisions_requested"  # the work state offered after a result that needs an answer
# Slot statuses (phase C1, fixed in code): needed (nothing yet), in_progress (a file arrived),
# final (marked by a user; needs a file), not_needed (marked by a user; counts as done).
NEEDED, IN_PROGRESS, FINAL, NOT_NEEDED = "needed", "in_progress", "final", "not_needed"
SLOT_STATUSES = (NEEDED, IN_PROGRESS, FINAL, NOT_NEEDED)
MAX_DOCUMENTS = 60  # live slots per step (a list has at most 50, plus the user's own)
MAX_DOC_NOTE = 2000
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
    site_id: int | None = None  # the site (hospital + PI) it is the IRB lifecycle of (phase B2a)


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
    # phase B2b
    parent_step_id: int | None = None  # set on a follow-up: the main step it answers
    sequence_no: int | None = None  # "2" in 2차 변경심의 (numbered step types)
    subject: str = ""  # what the step is about: "PT 2.0, CRF 1.1"
    result_key: str | None = None  # the IRB's answer (step_results); None: none yet
    target_session_id: int | None = None  # the IRB meeting it aims for (CAL-1)
    copied_from_step_id: int | None = None  # phase C2: the sibling site's step it was copied from
    # The user's own dates: [{"id", "label", "day"}] (migration step_custom_dates)
    custom_dates: tuple[dict, ...] = ()

    @property
    def label(self) -> str:
        """The name as the pages show it: "2차 변경심의" for a numbered step."""
        return f"{self.sequence_no}차 {self.name}" if self.sequence_no else self.name


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
    """A document slot of a step (phase C1): every upload is a version, the newest first."""

    id: int
    step_id: int
    name: str
    created_at: str
    deleted_at: str | None = None
    versions: tuple[DocVersion, ...] = ()  # newest first
    kind_id: int | None = None  # the list row it was copied from; None: added by a user
    section: str = "submission"
    origin: str = "upload"
    required: bool = False
    status: str = NEEDED
    position: int = 0
    note: str = ""
    updated_at: str = ""
    # Copied from a document list: name and section stay and it is not removed (the user marks
    # it not needed). Kept when the list row is deleted later (kind_id is then None).
    listed: bool = False
    copied_from_document_id: int | None = None  # phase C2: the slot it was copied from

    @property
    def done(self) -> bool:
        """Counts as done for the checklist: marked final, or not needed."""
        return self.status in (FINAL, NOT_NEEDED)


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
    """A required slot (phase C1): done when final or not needed."""

    name: str
    done: bool
    status: str = NEEDED
    document_id: int | None = None


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
        return sum(1 for c in self.checklist if not c.done)


@dataclass(frozen=True)
class PipelineSummary:
    pipeline: Pipeline
    hospital_name: str | None  # None: the hospital row is gone (should not happen)
    in_project: bool
    steps: int
    closed_steps: int
    site: Site | None = None


@dataclass(frozen=True)
class PipelineDetail:
    summary: PipelineSummary
    steps: list[StepView]
    statuses: list[StepStatus]
    step_types: list[StepType] = field(default_factory=list)
    access: str = "owner"  # the user's level on the project (read = change nothing)
    results: list[StepResult] = field(default_factory=list)  # phase B2b, archived ones too (labels)


@dataclass(frozen=True)
class StepPage:
    """One step with its documents (phase A: one step per page). previous / next follow the
    overview's order over live steps; a removed step has neither."""

    summary: PipelineSummary
    view: StepView
    previous_step_id: int | None
    next_step_id: int | None
    statuses: list[StepStatus]
    access: str = "owner"
    # phase B2b
    results: list[StepResult] = field(default_factory=list)
    parent: Step | None = None  # the main step a follow-up answers
    follow_ups: list[Step] = field(default_factory=list)  # live follow-ups of this step
    step_types: list[StepType] = field(default_factory=list)  # live follow-up types, to add one
    suggested_status: str | None = None  # offered after a result (owner: the user confirms)
    needs_answer: bool = False  # the result asks for an answer: "Add follow-up" is highlighted
    # CAL-1: the meeting the step aims for, the hospital's upcoming sessions and the hint
    target_session: Session | None = None
    sessions: list[Session] = field(default_factory=list)
    suggested_session_id: int | None = None
    target_warning: str | None = None  # cancelled | rejected | removed | unavailable
    session_kinds: list[SessionKind] = field(default_factory=list)  # archived too (labels)
    copied_from: "StepOrigin | None" = None  # phase C2


@dataclass(frozen=True)
class StepOrigin:
    """The step a copied step came from (phase C2), for "copied from <site>"."""

    pipeline_id: int
    step_id: int
    step_label: str
    site_label: str  # "" for an unnamed site
    removed: bool  # the step, its pipeline or its site is removed


@dataclass(frozen=True)
class HospitalSite:
    """One site of the hospital page: its pipeline's live main steps (follow-ups are counted)."""

    site: Site
    pipeline_id: int | None  # None: no live pipeline started yet
    steps: list[Step]
    follow_ups: dict[int, int]  # main step id -> live follow-ups


@dataclass(frozen=True)
class HospitalPage:
    """A hospital (IRB) of a project with its sites side by side (phase B2b)."""

    project_id: int
    hospital_id: int
    hospital_name: str | None
    sites: list[HospitalSite]
    statuses: list[StepStatus]
    results: list[StepResult]
    step_types: list[StepType]  # every type (archived too), to name and order the rows
    access: str = "read"


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
    project_owner_id: str | None = None
    site_label: str = ""  # "01 · PI 김○○" (phase B2a; empty for an unnamed site)
    site_id: int | None = None  # CAL-1: the calendar filters by site
    target_session_id: int | None = None  # CAL-1: the meeting it aims for
    result_key: str | None = None  # the IRB's answer, if recorded


class SessionTargets(Protocol):
    """services.sessions.SessionService: a step's target meeting (CAL-1)."""

    def check_target(self, session_id: int, hospital_id: int) -> Session: ...
    def upcoming_for(self, hospital_id: int) -> tuple[list[Session], Session | None]: ...
    def get_many(self, session_ids: list[int]) -> dict[int, Session]: ...
    def kinds(self, include_archived: bool = False) -> list[SessionKind]: ...


class ProjectAccess(Protocol):
    """services.project_access.ProjectAccessService."""

    def level(self, user_id: str, project_id: int) -> str | None: ...
    def workable_project_ids(self, user_id: str) -> list[int]: ...
    def pms(self, project_ids: list[int]) -> dict[int, str]: ...


class PipelineStore(Protocol):
    # projects and hospitals
    def project_owner(self, project_id: int) -> str | None: ...
    def project_hospital_ids(self, project_id: int) -> set[int]: ...
    def hospitals_of_projects(self, project_ids: list[int]) -> dict[int, set[int]]: ...
    def targeted_steps(self, skip_statuses: list[str]) -> list[Step]:
        """Live steps with a target meeting whose status is not in skip_statuses (CAL-1)."""
        ...
    def hospital_names(self, hospital_ids: list[int]) -> dict[int, str]: ...
    def projects_info(self, project_ids: list[int]) -> dict[int, tuple[str, str]]:
        """project id -> (name, owner id)."""
        ...
    # pipelines
    def pipelines_of_project(self, project_id: int) -> list[Pipeline]: ...
    def live_pipelines_of_user(self, user_id: str) -> list[Pipeline]: ...
    def live_pipelines_of_projects(self, project_ids: list[int]) -> list[Pipeline]: ...
    def live_pipelines(self, pipeline_ids: list[int]) -> list[Pipeline]: ...
    def due_steps(self, until: str, skip_statuses: list[str]) -> list[Step]:
        """Live steps with a due date on or before `until` whose status is not in skip_statuses
        (any pipeline, removed ones too; the caller keeps those of live pipelines)."""
        ...
    def get_pipeline(self, pipeline_id: int) -> Pipeline | None: ...
    def create_pipeline(
        self, user_id: str, project_id: int, hospital_id: int, steps: list[dict], site_id: int | None = None
    ) -> Pipeline:
        """A step dict may hold "documents": its slots (as add_step); nothing is left behind
        when a part fails."""
        ...
    def set_pipeline_deleted(self, pipeline_id: int, deleted_at: str | None) -> Pipeline: ...
    def touch_pipeline(self, pipeline_id: int) -> None: ...
    # steps
    def steps(self, pipeline_ids: list[int]) -> list[Step]: ...
    def get_step(self, step_id: int) -> Step | None: ...
    def version_text(self, version_id: int) -> str | None:
        """The text read from one uploaded version (None: no text was read)."""
        ...

    def add_step(self, pipeline_id: int, values: dict, documents: list[dict] | None = None) -> Step:
        """The step and its slots (phase C1: `documents` are step_documents values without
        step_id); a failed slot insert leaves no step behind."""
        ...
    def update_step(self, step_id: int, values: dict) -> Step: ...
    def reorder_steps(self, pipeline_id: int, step_ids: list[int]) -> None:
        """All or nothing; raises StepListError if the list is not exactly the live steps."""
        ...
    # documents (slots, phase C1)
    def documents(self, step_ids: list[int]) -> list[StepDocument]: ...
    def create_document(self, step_id: int, name: str, values: dict | None = None) -> StepDocument:
        """values: section, origin, required, status, position, kind_id (defaults otherwise).
        NameTakenError when a live slot of the step has the name."""
        ...
    def update_document(self, document_id: int, values: dict) -> StepDocument:
        """NameTakenError on a rename to a live slot's name; LookupError when gone."""
        ...
    def reorder_documents(self, step_id: int, document_ids: list[int]) -> None:
        """All or nothing; raises StepListError if the list is not exactly the live slots."""
        ...
    def delete_empty_document(self, document_id: int) -> None: ...
    def set_document_deleted(self, document_id: int, deleted_at: str | None) -> StepDocument: ...
    def add_version(self, document_id: int, version: NewVersion) -> DocVersion:
        """Raises NameTakenError if another upload took the same version number."""
        ...
    # history
    # copy to sibling sites (phase C2)
    def copy_step(self, step_id: int, actor_id: str, targets: list[dict], skipped: list[dict]) -> list[dict]:
        """Writes every target of step_copy.plan_copy in one transaction with the history rows
        (function copy_pipeline_step); returns [{pipeline_id, step_id, mode}]. Raises
        StepCopyError with the reason when something changed meanwhile; nothing is written then."""
        ...
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
    site_details: tuple[str, ...] = ()  # names of the site details a draft sends (D18; never values)


class PipelineFiles(Protocol):
    def put(self, path: str, content: bytes) -> None: ...
    def delete(self, paths: list[str]) -> None: ...
    def sign(self, path: str, filename: str, expires_in: int) -> str: ...


class NameTakenError(Exception):
    """Raised by the store when a live document or pipeline with the same key exists."""


class StepListError(Exception):
    """Raised by the store when a new step order is not exactly the pipeline's live steps."""


class StepParentError(Exception):
    """Raised by the store when the database refuses a follow-up's parent (one live level, same
    pipeline: trigger check_step_parent and the composite key), or the removal of a step that
    has live follow-ups (trigger check_step_removal). The message says why."""


class StepCopyError(Exception):
    """Raised by the store when a copy to sibling sites was refused because something changed
    meanwhile (phase C2). The message says what; nothing was written."""


PURGE_KINDS = ("pipelines", "steps", "documents")


def _clean(value: str, limit: int, what: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValidationError(f"the {what} is empty")
    if len(value) > limit:
        raise ValidationError(f"the {what} may have at most {limit} characters")
    return value


def _subject(value: str) -> str:
    """What a step is about ("PT 2.0, CRF 1.1"): one line, may be empty."""
    value = " ".join(value.split())
    if len(value) > MAX_SUBJECT:
        raise ValidationError(f"the subject may have at most {MAX_SUBJECT} characters")
    return value


def _date(value: str | None, what: str) -> str | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValidationError(f"the {what} must look like 2026-11-30") from exc


def _custom_dates(value: object) -> tuple[dict, ...]:
    """The step's own dates as sent (the whole list): each has a label and a day (the day may be
    empty). Ids are kept, or made here; the list is cleaned so nothing else is stored."""
    if not isinstance(value, list | tuple):
        raise ValidationError("the own dates must be a list")
    if len(value) > MAX_CUSTOM_DATES:
        raise ValidationError(f"a step may have at most {MAX_CUSTOM_DATES} own dates")
    out: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValidationError("each own date needs a label and a day")
        label = _clean(str(item.get("label") or ""), MAX_DATE_LABEL, "date name")
        day = _date(item.get("day"), f"date of {label}")
        raw_id = "".join(ch for ch in str(item.get("id") or "") if ch.isalnum() or ch in "-_")[:24]
        key = raw_id if raw_id and raw_id not in seen else secrets.token_hex(4)
        seen.add(key)
        out.append({"id": key, "label": label, "day": day})
    return tuple(out)


def safe_filename(filename: str) -> str:
    """The file name for storage paths: the base name only, unsafe characters replaced."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    stem = _UNSAFE.sub("_", stem).strip("._") or "file"
    ext = _UNSAFE.sub("", ext)[:10]
    return f"{stem[:80]}.{ext}" if ext else stem[:80]


def is_main(step: Step, live_ids: set[int]) -> bool:
    """A main step, or a live follow-up whose main step is not live (it was purged, or removed
    in a race the triggers did not see): then it is shown as a main step everywhere."""
    return step.parent_step_id is None or step.parent_step_id not in live_ids


def _groups(live: list[Step]) -> list[list[Step]]:
    """Live steps in the given order, grouped as [main, its follow-ups...]."""
    ids = {s.id for s in live}
    return [[m, *(s for s in live if not is_main(s, ids) and s.parent_step_id == m.id)] for m in live if is_main(m, ids)]


def display_order(steps: list[Step]) -> list[Step]:
    """Live main steps by position, each followed by its live follow-ups (by position); then
    removed steps."""

    def key(s: Step) -> tuple[int, int]:
        return (s.position, s.id)

    live = sorted((s for s in steps if s.deleted_at is None), key=key)
    return [s for group in _groups(live) for s in group] + sorted((s for s in steps if s.deleted_at is not None), key=key)


def slots_from(kinds: list[DocumentKind]) -> list[dict]:
    """The slots a new step gets from its document list (phase C1), in the list's order."""
    return [
        {
            # required as the list has it: saving a list clears it for NEVER_REQUIRED sections
            "name": k.name, "kind_id": k.id, "listed": True, "section": k.section, "origin": k.origin,
            "required": k.required, "status": NEEDED, "position": n,
        }
        for n, k in enumerate(kinds, start=1)
    ]


_SECTION_ORDER = {s: i for i, s in enumerate(("application", "received", "submission", "reference"))}


def slot_order(docs: list[StepDocument]) -> list[StepDocument]:
    """Live slots by section (신청서 및 통지서, 제출서류, 참고자료) and position; removed ones last."""
    return sorted(
        docs, key=lambda d: (d.deleted_at is not None, _SECTION_ORDER.get(d.section, 9), d.position, d.id)
    )


def checklist(docs: list[StepDocument]) -> list[ChecklistItem]:
    """Required live slots. Only listed slots are required, and a list never marks documents
    from the IRB or reference ones required (PipelineSetupService._clean_kinds)."""
    return [ChecklistItem(d.name, d.done, d.status, d.id) for d in slot_order(docs) if d.deleted_at is None and d.required]


def suggested_status(step: Step, result: StepResult | None, statuses: list[StepStatus]) -> str | None:
    """The status the step page offers after the IRB result is set (owner, 2026-11-14: offered,
    never set by itself). A result that needs an answer offers 보완요청 (the work goes on); a
    final answer (approved, or rejected) offers 완료, or the first live closed status when 완료
    is gone. None when nothing fits or the step already has it."""
    if step.deleted_at is not None or result is None:
        return None
    live = [s for s in statuses if s.archived_at is None]
    if result.needs_answer:
        target = next((s.key for s in live if s.key == ANSWERING), None)
    else:
        target = next((s.key for s in live if s.key == DONE), None) or next((s.key for s in live if s.closed), None)
    return target if target and target != step.status_key else None


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
        access: ProjectAccess | None = None,
        sites: SiteStore | None = None,
        sessions: SessionTargets | None = None,
    ) -> None:
        self._sessions = sessions
        self._sites = sites
        self._users = users
        self._store = store
        self._setup = setup
        self._files = files
        self._max_bytes = max_file_bytes
        self._ttl = link_ttl_seconds
        self._tz = ZoneInfo(timezone)
        self._clock = clock
        self._access = access

    # -- access (phase B1: whoever may open the project; services/project_access.py) ----------

    def _user(self, user_id: str) -> bool:
        """The user's account is active; returns whether they are a platform admin."""
        return active_profile(self._users, user_id, "your account is not active").role == "admin"

    def level(self, user_id: str, project_id: int) -> str | None:
        """The user's level on the project (owner, manage, pm, edit, read) or None. Without an
        access service (older tests): the owner only."""
        if self._access is not None:
            return self._access.level(user_id, project_id)
        return "owner" if self._store.project_owner(project_id) == user_id else None

    def _check(self, user_id: str, project_id: int, need: str, missing: str) -> str:
        level = self.level(user_id, project_id)
        if level is None:
            raise NotFoundError(missing)
        if level not in NEEDS[need]:
            raise ForbiddenError(REFUSALS[need])
        return level

    def _own_project(self, user_id: str, project_id: int, need: str = "edit") -> str:
        return self._check(user_id, project_id, need, "project not found")

    def _pipeline(self, user_id: str, pipeline_id: int, *, live: bool = True, need: str = "edit") -> Pipeline:
        return self._pipeline_at(user_id, pipeline_id, live=live, need=need)[0]

    def _pipeline_at(
        self, user_id: str, pipeline_id: int, *, live: bool = True, need: str = "edit"
    ) -> tuple[Pipeline, str]:
        """The pipeline and the user's level on its project, checked once."""
        self._user(user_id)
        pipeline = self._store.get_pipeline(pipeline_id)
        if pipeline is None:
            raise NotFoundError("pipeline not found")
        level = self._check(user_id, pipeline.project_id, need, "pipeline not found")
        if live and pipeline.deleted_at is not None:
            raise ConflictError("this pipeline is removed; restore it first")
        return pipeline, level

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
        except StepParentError as exc:  # a follow-up was added at the same moment (trigger)
            raise ConflictError(f"{exc}; reload the page") from exc

    def _first_open_status(self) -> str:
        status = next((s for s in self._setup.statuses() if not s.closed), None)
        if status is None:
            raise ConflictError("no open step status is set; ask an admin")
        return status.key

    # -- pipelines ----------------------------------------------------------------------------

    def _summaries(
        self,
        project_id: int,
        pipelines: list[Pipeline],
        loaded: list[Step] | None = None,
        prefetched: tuple[set[int], dict[int, str], list[Site]] | None = None,
    ) -> list[PipelineSummary]:
        """`loaded`: the steps of these pipelines, when the caller already read them (the step
        page), so they are not read twice. `prefetched`: the project's hospital ids, the hospital
        names and the sites, when the caller read them at the same time as other things."""
        if prefetched is None:
            in_project = self._store.project_hospital_ids(project_id)
            names = self._store.hospital_names([p.hospital_id for p in pipelines])
            site_ids = sorted({p.site_id for p in pipelines if p.site_id is not None})
            site_rows = self._sites.sites(site_ids) if self._sites is not None and site_ids else []
        else:
            in_project, names, site_rows = prefetched
        closed = {s.key for s in self._setup.statuses(include_archived=True) if s.closed}
        steps = [
            s for s in (loaded if loaded is not None else self._store.steps([p.id for p in pipelines]))
            if s.deleted_at is None
        ]
        sites = {s.id: s for s in site_rows}
        out = []
        for p in pipelines:
            own = [s for s in steps if s.pipeline_id == p.id]
            out.append(
                PipelineSummary(
                    p, names.get(p.hospital_id), p.hospital_id in in_project, len(own),
                    sum(1 for s in own if s.status_key in closed), sites.get(p.site_id) if p.site_id else None,
                )
            )
        return out

    def list_for_project(self, user_id: str, project_id: int, include_removed: bool = False) -> list[PipelineSummary]:
        self._user(user_id)
        self._own_project(user_id, project_id, "read")
        # Every pipeline of the project, whoever started it (shared projects, phase B1).
        pipelines = [
            p for p in self._store.pipelines_of_project(project_id) if include_removed or p.deleted_at is None
        ]
        return self._summaries(project_id, pipelines)

    def _site_for_start(self, user_id: str, project_id: int, hospital_id: int | None, site_id: int | None) -> tuple[int, int | None]:
        """(hospital, site) for a new pipeline (phase B2a). With a site: that site. With a
        hospital alone: its unnamed site, made when the hospital has no site yet; a hospital
        with named PIs starts per site only (owner, 2026-11-13)."""
        if self._sites is None:  # before sites (older tests): the database picks the site
            if hospital_id is None:
                raise ValidationError("choose a hospital or a site")
            return hospital_id, None
        if site_id is not None:
            site = self._sites.get_site(site_id)
            if site is None or site.project_id != project_id:
                raise NotFoundError("site not found")
            if site.deleted_at is not None:
                raise ConflictError("this site is removed; restore it first")
            return site.hospital_id, site.id
        if hospital_id is None:
            raise ValidationError("choose a hospital or a site")
        if hospital_id not in self._store.project_hospital_ids(project_id):
            raise ValidationError("this hospital is not in the project; add it first")
        live = [s for s in self._sites.sites_of_project(project_id) if s.hospital_id == hospital_id and s.deleted_at is None]
        if any(s.pi_name.strip() for s in live):
            raise ConflictError("this hospital has PIs; start the pipeline of a PI's site")
        unnamed = next((s for s in live if not s.pi_name.strip()), None)
        if unnamed is not None:
            return hospital_id, unnamed.id
        try:
            site = self._sites.create_site(project_id, hospital_id, user_id, "", "", "")
        except LookupError as exc:
            raise ConflictError("a site was just added for this hospital; reload the page") from exc
        return hospital_id, site.id

    def start(self, user_id: str, project_id: int, hospital_id: int | None = None, site_id: int | None = None) -> Pipeline:
        """A pipeline for a site (phase B2a): one live pipeline per site. A hospital alone
        starts its unnamed site's pipeline."""
        self._user(user_id)
        self._own_project(user_id, project_id)
        hospital_id, site_id = self._site_for_start(user_id, project_id, hospital_id, site_id)
        if hospital_id not in self._store.project_hospital_ids(project_id):
            raise ValidationError("this hospital is not in the project; add it first")
        if any(
            p.deleted_at is None and (p.site_id == site_id if site_id is not None else p.hospital_id == hospital_id)
            for p in self._store.pipelines_of_project(project_id)
        ):
            raise ConflictError("this site already has a pipeline")
        order = self._setup.template_for(hospital_id)
        # Follow-up types answer another step: never started on their own (phase B2b).
        live = {t.id: t for t in self._setup.step_types() if t.category != FOLLOW_UP}
        status = self._first_open_status()
        type_ids = [t for t in order.step_type_ids if t in live]
        kinds = self._setup.document_kinds_for(type_ids, hospital_id)  # phase C1: each step's slots
        steps = []
        seen: dict[int, int] = {}  # numbered types: 1차, 2차, ... in the order's sequence
        for i, tid in enumerate(type_ids, start=1):
            seen[tid] = seen.get(tid, 0) + 1
            row = {
                "step_type_id": tid, "name": live[tid].name, "position": i, "status_key": status,
                "documents": slots_from(kinds.get(tid, [])),
            }
            if live[tid].numbered:
                row["sequence_no"] = seen[tid]
            steps.append(row)
        try:
            pipeline = self._store.create_pipeline(user_id, project_id, hospital_id, steps, site_id)
        except NameTakenError as exc:
            raise ConflictError("this site already has a pipeline") from exc
        self._event(
            pipeline.id, None, user_id, "pipeline.started",
            {
                "hospital_id": hospital_id, "site_id": site_id, "steps": [s["name"] for s in steps],
                "from": "hospital" if order.hospital_id else "global",
            },
        )
        return pipeline

    def _views(
        self,
        steps: list[Step],
        documents: list[StepDocument] | None = None,
        types: list[StepType] | None = None,
    ) -> list[StepView]:
        """`documents` and `types` (all, archived too): when the caller read them already."""
        by_step: dict[int, list[StepDocument]] = {}
        if documents is None:
            documents = self._store.documents([s.id for s in steps])
        for d in documents:  # grouped once, not per step
            by_step.setdefault(d.step_id, []).append(d)
        if types is None:
            types = self._setup.step_types(include_archived=True)
        all_types = {t.id: t for t in types}
        views = []
        for step in steps:
            own = by_step.get(step.id, [])
            stype = all_types.get(step.step_type_id) if step.step_type_id else None
            views.append(StepView(step, stype.name if stype else None, slot_order(own), checklist(own)))
        return views

    def detail(self, user_id: str, pipeline_id: int) -> PipelineDetail:
        # The level comes with the pipeline and the steps are read once: the summary and the
        # overview's order share them (17 database calls before, owner's measurement 2026-11-17).
        pipeline, level = self._pipeline_at(user_id, pipeline_id, live=False, need="read")
        all_steps = self._store.steps([pipeline.id])
        summary = self._summaries(pipeline.project_id, [pipeline], all_steps)[0]
        views = self._views(display_order(all_steps))
        return PipelineDetail(
            summary, views, self._setup.statuses(include_archived=True), self._setup.step_types(),
            level, self._setup.results(include_archived=True),
        )

    def step_page(self, user_id: str, pipeline_id: int, step_id: int) -> StepPage:
        """One step for its own page. A removed step or a step of a removed pipeline still
        opens (read-only in the page; files stay downloadable); a stranger gets not found."""
        pipeline, level = self._pipeline_at(user_id, pipeline_id, live=False, need="read")
        # Everything below that does not depend on another answer is read at the same time (about
        # 15 calls one after another took 0.7 s warm and 1.3 s cold; owner's measurement
        # 2026-11-17). The steps are read once; the summary and the order use them.
        site_ids = [pipeline.site_id] if pipeline.site_id is not None else []
        (
            all_steps, statuses, results, all_types, in_project, names, site_rows, documents, hospital_sessions,
        ) = together(
            lambda: self._store.steps([pipeline.id]),
            lambda: self._setup.statuses(include_archived=True),
            lambda: self._setup.results(include_archived=True),
            lambda: self._setup.step_types(include_archived=True),
            lambda: self._store.project_hospital_ids(pipeline.project_id),
            lambda: self._store.hospital_names([pipeline.hospital_id]),
            lambda: self._sites.sites(site_ids) if self._sites is not None and site_ids else [],
            lambda: self._store.documents([step_id]),
            lambda: self._hospital_sessions(pipeline),
        )
        steps = display_order(all_steps)
        step = next((s for s in steps if s.id == step_id), None)
        if step is None:
            raise NotFoundError("step not found")
        previous_id = next_id = None
        if step.deleted_at is None:
            live = [s.id for s in steps if s.deleted_at is None]
            at = live.index(step.id)
            previous_id = live[at - 1] if at > 0 else None
            next_id = live[at + 1] if at + 1 < len(live) else None
        result = next((r for r in results if r.key == step.result_key), None)
        parent = next((s for s in steps if s.id == step.parent_step_id), None) if step.parent_step_id else None
        follow_ups = [s for s in steps if s.parent_step_id == step.id and s.deleted_at is None]
        # These two need the step: its target meeting and (for a copy) where it came from.
        target_read, copied_from = together(
            lambda: self._target_session(step),
            lambda: self._origin(pipeline, step),
        )
        target, sessions, suggested, warning, kinds = self._step_sessions_from(
            step, hospital_sessions, target_read
        )
        live_types = [t for t in all_types if t.archived_at is None]
        return StepPage(
            self._summaries(pipeline.project_id, [pipeline], all_steps, (in_project, names, site_rows))[0],
            self._views([step], documents, all_types)[0],
            previous_id,
            next_id,
            statuses,
            level,
            results=results,
            parent=parent,
            follow_ups=follow_ups,
            step_types=[t for t in live_types if t.category == FOLLOW_UP],
            suggested_status=suggested_status(step, result, statuses),
            # The step's own result counts even when the admin archived it since (as in
            # suggested_status): archiving stops new use, it does not change past answers.
            needs_answer=bool(result and result.needs_answer),
            target_session=target,
            sessions=sessions,
            suggested_session_id=suggested,
            target_warning=warning,
            session_kinds=kinds,
            copied_from=copied_from,
        )

    def _origin(self, pipeline: Pipeline, step: Step) -> StepOrigin | None:
        """Where a copied step came from (phase C2): three reads, only for a copy. None when the
        source was purged (the column is then null) or is not in this project."""
        if step.copied_from_step_id is None:
            return None
        source = self._store.get_step(step.copied_from_step_id)
        origin = self._store.get_pipeline(source.pipeline_id) if source is not None else None
        if source is None or origin is None or origin.project_id != pipeline.project_id:
            return None
        site = self._sites.sites([origin.site_id]) if self._sites is not None and origin.site_id is not None else []
        return StepOrigin(
            origin.id, source.id, source.label, site[0].label if site else "",
            source.deleted_at is not None or origin.deleted_at is not None
            or bool(site and site[0].deleted_at is not None),
        )

    # -- copy to sibling sites (phase C2; the rules are in services/step_copy.py) -----------

    def _copy_plan(self, user_id: str, pipeline_id: int, step_id: int) -> tuple[Step, list]:
        """The source step and step_copy.plan_copy's answer for every sibling site. Edit access,
        like any change. About 10-12 database reads (user, pipeline, access, sites, pipelines,
        steps, documents and versions, step types, statuses, the meeting), whatever the number
        of sites."""
        from irb_watch.services import step_copy  # it imports this module

        pipeline = self._pipeline(user_id, pipeline_id)
        if self._sites is None or pipeline.site_id is None:
            raise ConflictError("this server has no sites set up; a step cannot be copied")
        sites = [
            s for s in self._sites.sites_of_project(pipeline.project_id)
            if s.hospital_id == pipeline.hospital_id and s.deleted_at is None and s.id != pipeline.site_id
        ]
        pipes = {
            p.site_id: p for p in self._store.pipelines_of_project(pipeline.project_id)
            if p.deleted_at is None and p.site_id is not None and p.id != pipeline.id
        }
        siblings = [(s, pipes.get(s.id)) for s in sites]
        steps = self._store.steps([pipeline.id, *(p.id for _, p in siblings if p is not None)])
        step = next((s for s in steps if s.id == step_id and s.pipeline_id == pipeline.id), None)
        if step is None:
            raise NotFoundError("step not found")
        if step.deleted_at is not None:
            raise ConflictError("this step is removed; restore it first")
        if step.parent_step_id is not None:
            raise ValidationError("a follow-up step is not copied; add it at each site")
        others = [s for s in steps if s.pipeline_id != pipeline.id]
        candidates = [
            s.id for s in others
            if s.deleted_at is None and s.parent_step_id is None
            and step.step_type_id is not None and s.step_type_id == step.step_type_id
        ]
        docs = self._store.documents([step.id, *candidates])
        stype = next((t for t in self._setup.step_types(include_archived=True) if t.id == step.step_type_id), None)
        statuses = self._setup.statuses(include_archived=True)
        first = next((s.key for s in statuses if not s.closed and s.archived_at is None), None)
        if first is None:
            raise ConflictError("no open step status is set; ask an admin")
        meeting_ok = False
        meeting_day: str | None = None
        if step.target_session_id is not None and self._sessions is not None:
            try:
                meeting_day = self._sessions.check_target(step.target_session_id, pipeline.hospital_id).meets_on
                meeting_ok = True
            except (ValidationError, NotFoundError):
                meeting_ok = False
        targets = step_copy.plan_copy(
            step, [d for d in docs if d.step_id == step.id], siblings, others,
            [d for d in docs if d.step_id != step.id],
            numbered=bool(stype and stype.numbered), closed={s.key for s in statuses if s.closed},
            first_status=first, meeting_ok=meeting_ok, meeting_day=meeting_day,
        )
        return step, targets

    def copy_targets(self, user_id: str, pipeline_id: int, step_id: int) -> list:
        """What "Copy to other PIs" would do at each sibling site (nothing is written)."""
        return self._copy_plan(user_id, pipeline_id, step_id)[1]

    def copy_step(self, user_id: str, pipeline_id: int, step_id: int, site_ids: list[int]) -> list:
        """Copies the step to the chosen sibling sites, planned again here (the dialog's preview
        is not trusted). All or nothing. The answer has one row per chosen site: added, filled,
        or skipped with its reason; when every chosen site is skipped, nothing is written and
        the reasons are the error."""
        from irb_watch.services.step_copy import REASONS, SKIP

        if not site_ids:
            raise ValidationError("choose at least one site")
        step, targets = self._copy_plan(user_id, pipeline_id, step_id)
        by_site = {t.site.id: t for t in targets}
        if any(i not in by_site for i in site_ids):
            raise ValidationError("copy only to other sites of this hospital in the project")
        chosen = [by_site[i] for i in dict.fromkeys(site_ids)]
        doing = [t for t in chosen if t.action != SKIP]
        if not doing:
            raise ConflictError(
                "nothing was copied: "
                + "; ".join(f"{t.site_label or 'the unnamed site'}: {REASONS[t.reason or '']}" for t in chosen)
            )
        skipped = [{"site_id": t.site.id, "reason": t.reason} for t in chosen if t.action == SKIP]
        try:
            done = self._store.copy_step(step.id, user_id, [t.plan for t in doing], skipped)
        except StepCopyError as exc:
            raise ConflictError(f"{exc}; nothing was copied, reload and try again") from exc
        made = {int(r["pipeline_id"]): int(r["step_id"]) for r in done}
        # The copy function carries the meeting but not its day: set the day (IRB 심의일) here, only
        # where the step has none. Not part of the all-or-nothing write; a miss is logged and the
        # user can pick the meeting again.
        for t in doing:
            day = (t.plan or {}).get("step", {}).get("decided_on")
            sid = made.get(t.pipeline_id)
            if not day or sid is None:
                continue
            try:
                current = self._store.get_step(sid)
                if current is not None and not current.decided_on:
                    self._store.update_step(sid, {"decided_on": day, "updated_at": self._now()})
            except Exception as exc:  # noqa: BLE001
                log.warning("the meeting day of copied step %s could not be set: %s", sid, type(exc).__name__)
        return [replace(t, step_id=made.get(t.pipeline_id, t.step_id)) if t.action != SKIP else t for t in chosen]

    def _hospital_sessions(self, pipeline: Pipeline) -> tuple[list[Session], Session | None, list[SessionKind]] | None:
        """The hospital's upcoming sessions, the hint and the kinds (CAL-1). None when the read
        failed or there is no session service: the page then shows no sessions."""
        if self._sessions is None:
            return None
        try:
            sessions, hint = self._sessions.upcoming_for(pipeline.hospital_id)
            return sessions, hint, self._sessions.kinds(include_archived=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("the sessions of pipeline %s could not be read: %s", pipeline.id, type(exc).__name__)
            return None

    def _follows_target(self, step: Step, field_name: str) -> bool:
        """A step date (due_on, decided_on) may be replaced by a newly picked meeting's date: it is
        empty, or it equals the date the meeting the step aimed for until now gave it."""
        current = getattr(step, field_name)
        if not current:
            return True
        previous, failed = self._target_session(step)
        if failed or previous is None:
            return False
        return current == (previous.deadline if field_name == "due_on" else previous.meets_on)

    def _target_session(self, step: Step) -> tuple[Session | None, bool]:
        """The step's target meeting and whether the read failed."""
        if self._sessions is None or step.target_session_id is None:
            return None, False
        try:
            return self._sessions.get_many([step.target_session_id]).get(step.target_session_id), False
        except Exception as exc:  # noqa: BLE001
            log.warning("the target session of step %s could not be read: %s", step.id, type(exc).__name__)
            return None, True

    def _step_sessions_from(
        self,
        step: Step,
        hospital: tuple[list[Session], Session | None, list[SessionKind]] | None,
        target_read: tuple[Session | None, bool],
    ) -> tuple[Session | None, list[Session], int | None, str | None, list[SessionKind]]:
        """The step's target meeting, the hospital's upcoming sessions, the hint, the warning and
        the kinds. A failed read leaves them out with the warning 'unavailable' instead of
        failing the page."""
        if self._sessions is None:
            return None, [], None, None, []
        target, target_failed = target_read
        if hospital is None or target_failed:
            return None, [], None, "unavailable", []
        sessions, hint, kinds = hospital
        warning = target_warning(target)
        if step.target_session_id is not None and target is None:
            warning = "removed"
        return target, sessions, hint.id if hint is not None else None, warning, kinds

    # -- the hospital page (phase B2b) -------------------------------------------------------

    def hospital_page(self, user_id: str, project_id: int, hospital_id: int) -> HospitalPage:
        """A hospital of the project with its live sites side by side: each site's live main
        steps (type, number, status, result, dates). Also for a hospital that left the project
        while it still has sites. The page is linked only for a hospital with 2 or more sites
        (owner); any hospital of the project answers."""
        self._user(user_id)
        level = self._own_project(user_id, project_id, "read")
        sites = (
            [s for s in self._sites.sites_of_project(project_id) if s.hospital_id == hospital_id and s.deleted_at is None]
            if self._sites is not None else []
        )
        if not sites and hospital_id not in self._store.project_hospital_ids(project_id):
            raise NotFoundError("hospital not found in this project")
        pipelines = {
            p.site_id: p for p in self._store.pipelines_of_project(project_id)
            if p.deleted_at is None and p.hospital_id == hospital_id and p.site_id is not None
        }
        steps = [s for s in self._store.steps([p.id for p in pipelines.values()]) if s.deleted_at is None]
        out = []
        for site in sorted(sites, key=lambda s: (s.site_no or "~", s.pi_name.lower(), s.id)):
            p = pipelines.get(site.id)
            own = [s for s in steps if p is not None and s.pipeline_id == p.id]
            ids = {s.id for s in own}  # live steps; the same rule as the overview (is_main)
            mains = sorted((s for s in own if is_main(s, ids)), key=lambda s: (s.position, s.id))
            counts: dict[int, int] = {}
            for s in own:
                if not is_main(s, ids):
                    counts[s.parent_step_id] = counts.get(s.parent_step_id, 0) + 1  # type: ignore[index]
            out.append(HospitalSite(site, p.id if p else None, mains, counts))
        return HospitalPage(
            project_id, hospital_id, self._store.hospital_names([hospital_id]).get(hospital_id), out,
            self._setup.statuses(include_archived=True), self._setup.results(include_archived=True),
            self._setup.step_types(include_archived=True), level,
        )

    # -- work across projects (phase A2) ------------------------------------------------------

    def _work_steps(self, pipelines: list[Pipeline], steps: list[Step], views: dict[int, StepView]) -> list[WorkStep]:
        by_id = {p.id: p for p in pipelines}
        hospitals = self._store.hospital_names(sorted({p.hospital_id for p in pipelines}))
        projects = self._store.projects_info(sorted({p.project_id for p in pipelines}))
        statuses = {s.key: s for s in self._setup.statuses(include_archived=True)}
        site_ids = sorted({p.site_id for p in pipelines if p.site_id is not None})
        labels = {s.id: s.label for s in self._sites.sites(site_ids)} if self._sites is not None and site_ids else {}
        out = []
        for s in steps:
            p = by_id[s.pipeline_id]
            status = statuses.get(s.status_key)
            view = views.get(s.id)
            name, owner = projects.get(p.project_id, (None, None))
            out.append(
                WorkStep(
                    p.id, p.project_id, name, p.hospital_id, hospitals.get(p.hospital_id),
                    s.id, s.label, s.status_key, status.label if status else s.status_key,
                    bool(status and status.closed), s.due_on, s.submitted_on, s.decided_on,
                    len(view.checklist) if view else 0, view.required_missing if view else 0,
                    owner, labels.get(p.site_id, "") if p.site_id is not None else "",
                    p.site_id, s.target_session_id, s.result_key,
                )
            )
        return out

    def calendar_steps(self, user_id: str) -> list[WorkStep]:
        """Every live step of the live pipelines of the projects the user may work on (as the
        work page), without reading documents. For the calendar (CAL-1)."""
        return self.calendar_scope(user_id)[0]

    def calendar_scope(self, user_id: str) -> tuple[list[WorkStep], list[int], dict[int, set[int]]]:
        """The calendar's steps, its projects (workable_project_ids) and project -> hospitals
        (workable_hospitals_by_project), reading the access rule and the pipelines once."""
        self._user(user_id)
        project_ids, pipelines = self._workable(user_id)
        by_project = self._hospitals_by_project(project_ids, pipelines)
        if not pipelines:
            return [], project_ids, by_project
        steps = [s for s in self._store.steps([p.id for p in pipelines]) if s.deleted_at is None]
        return self._work_steps(pipelines, steps, {}), project_ids, by_project

    def _workable(self, user_id: str) -> tuple[list[int], list[Pipeline]]:
        """The projects the user may work on and their live pipelines."""
        if self._access is not None:
            project_ids = self._access.workable_project_ids(user_id)
            return project_ids, self._store.live_pipelines_of_projects(project_ids) if project_ids else []
        pipelines = self._store.live_pipelines_of_user(user_id)
        return sorted({p.project_id for p in pipelines}), pipelines

    def _hospitals_by_project(self, project_ids: list[int], pipelines: list[Pipeline]) -> dict[int, set[int]]:
        if not project_ids:
            return {}
        out = {p: set(h) for p, h in self._store.hospitals_of_projects(project_ids).items()}
        for p in pipelines:
            out.setdefault(p.project_id, set()).add(p.hospital_id)
        return out

    def workable_project_ids(self, user_id: str) -> list[int]:
        """The projects the calendar and the reminders cover (owner, manage, pm, edit)."""
        if self._access is not None:
            return self._access.workable_project_ids(user_id)
        return sorted({p.project_id for p in self._store.live_pipelines_of_user(user_id)})

    def workable_hospital_ids(self, user_id: str) -> set[int]:
        """Hospitals of the projects the user may work on: in the project, or of one of its live
        pipelines (a hospital may leave a project while its pipeline goes on). Who may propose
        a session for a hospital (CAL-1), and the calendar's default hospitals."""
        return set().union(set(), *self.workable_hospitals_by_project(user_id).values())

    def workable_hospitals_by_project(self, user_id: str) -> dict[int, set[int]]:
        """project -> its hospitals (in the project, or of one of its live pipelines), for the
        projects the user may work on."""
        project_ids = self.workable_project_ids(user_id)
        if not project_ids:
            return {}
        return self._hospitals_by_project(project_ids, self._store.live_pipelines_of_projects(project_ids))

    def project_owners(self, project_ids: list[int]) -> dict[int, tuple[str, str | None]]:
        """project -> (name, owner id), for the calendar and the reminders."""
        return {k: (v[0], v[1]) for k, v in self._store.projects_info(sorted(set(project_ids))).items()}

    def project_pms(self, project_ids: list[int]) -> dict[int, str]:
        return self._access.pms(sorted(set(project_ids))) if self._access is not None and project_ids else {}

    def hospital_names(self, hospital_ids: list[int]) -> dict[int, str]:
        return self._store.hospital_names(sorted(set(hospital_ids))) if hospital_ids else {}

    def cutoff_steps(self) -> list[WorkStep]:
        """Live steps of live pipelines that aim for a meeting and are still open and not
        submitted: the cutoff reminders (CAL-1). Documents are not read."""
        closed = [s.key for s in self._setup.statuses(include_archived=True) if s.closed]
        steps = self._store.targeted_steps(sorted({*closed, SUBMITTED}))
        if not steps:
            return []
        pipelines = self._store.live_pipelines(sorted({s.pipeline_id for s in steps}))
        live = {p.id for p in pipelines}
        return self._work_steps(pipelines, [s for s in steps if s.pipeline_id in live and s.deleted_at is None], {})

    def work_steps(self, user_id: str) -> list[WorkStep]:
        """Every live step of the live pipelines of every project the user may work on (owner,
        organization admin, PM, member; phase B1), over all projects (pipelines whose hospital
        left the project too), with its checklist counts. For the work page."""
        self._user(user_id)
        if self._access is not None:
            pipelines = self._store.live_pipelines_of_projects(self._access.workable_project_ids(user_id))
        else:
            pipelines = self._store.live_pipelines_of_user(user_id)
        if not pipelines:
            return []
        steps = [s for s in self._store.steps([p.id for p in pipelines]) if s.deleted_at is None]
        views = {v.step.id: v for v in self._views(steps)}
        return self._work_steps(pipelines, steps, views)

    def reminder_steps(self, until: str) -> dict[str, list[WorkStep]]:
        """Live steps of live pipelines with a due date on or before `until`, per recipient: the
        project's owner and its PM (owner, 2026-11-13, O27). Documents are not read. Run by the
        daily digest; the caller decides which steps are due for a reminder."""
        # Finished and submitted steps never get a reminder: leave them out in the query, so the
        # daily read does not grow with the history.
        closed = [s.key for s in self._setup.statuses(include_archived=True) if s.closed]
        steps = self._store.due_steps(until, sorted({*closed, SUBMITTED}))
        if not steps:
            return {}
        pipelines = self._store.live_pipelines(sorted({s.pipeline_id for s in steps}))
        live = {p.id for p in pipelines}
        steps = [s for s in steps if s.pipeline_id in live and s.deleted_at is None]
        project_ids = sorted({p.project_id for p in pipelines})
        pms = self._access.pms(project_ids) if self._access is not None else {}
        out: dict[str, list[WorkStep]] = {}
        for w in self._work_steps(pipelines, steps, {}):
            for recipient in {w.project_owner_id, pms.get(w.project_id)} - {None}:
                out.setdefault(recipient, []).append(w)  # type: ignore[arg-type]
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
        site_no, pi_name, facts = self._site_details(pipeline)
        sent = site_detail_names(site_no, pi_name, sorted(facts))
        return DraftInputs(
            step.id, step.name, pipeline.hospital_id, files,
            allow_board_documents=stype is None or stype.input_board_documents,
            guide_ids=tuple(stype.guide_ids) if stype else (),
            form_template_id=stype.form_template_id if stype else None,
            has_instructions=bool(stype and stype.instructions.strip()),
            site_details=tuple(sent),
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
        site_no, pi_name, facts = self._site_details(pipeline)
        return StepContext(
            step_id=step.id,
            step_name=step.name,
            hospital_id=pipeline.hospital_id,
            files=tuple(out),
            instructions=stype.instructions if stype else "",
            guide_ids=tuple(stype.guide_ids) if stype else (),
            form_template_id=stype.form_template_id if stype else None,
            allow_board_documents=stype is None or stype.input_board_documents,
            site_no=site_no,
            pi_name=pi_name,
            site_facts=tuple(sorted(facts.items())),
        )

    def _site_details(self, pipeline: Pipeline) -> tuple[str, str, dict[str, str]]:
        """The site's number, PI and current details (D18: always sent with a pipeline draft)."""
        if self._sites is None or pipeline.site_id is None:
            return "", "", {}
        site = self._sites.get_site(pipeline.site_id)
        if site is None:
            return "", "", {}
        return site.site_no, site.pi_name, current_facts(self._sites.facts([site.id]), self.today())

    def check_own_step(self, user_id: str, step_id: int, need: str = "edit") -> None:
        """Raises "step not found" unless the user may open the step's project, and 403 when
        their level is too low for `need` (before a job row names the step)."""
        found = self.step_access(user_id, step_id)
        self._user(user_id)
        if found is None:
            raise NotFoundError("step not found")
        if found[1] not in NEEDS[need]:
            raise ForbiddenError(REFUSALS[need])

    def step_exists(self, step_id: int) -> bool:
        return self._store.get_step(step_id) is not None

    def step_access(self, user_id: str, step_id: int) -> tuple[int, str] | None:
        """The one place that says whether a step (found by id alone) may be opened: its
        pipeline id and the user's level on the project, or None."""
        step = self._store.get_step(step_id)
        pipeline = self._store.get_pipeline(step.pipeline_id) if step is not None else None
        if pipeline is None:
            return None
        level = self.level(user_id, pipeline.project_id)
        return (pipeline.id, level) if level is not None else None

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
        """The pipeline holding this step, when the user may open its project (removed ones too);
        None otherwise. For the draft page's "Back to step" link only, so it never raises: a
        failed read leaves the link out instead of failing the draft page."""
        try:
            found = self.step_access(user_id, step_id)
            return found[0] if found else None
        except Exception as exc:  # noqa: BLE001
            log.warning("the pipeline of step %s could not be read: %s", step_id, type(exc).__name__)
            return None

    def step_ids(self, user_id: str, pipeline_id: int) -> list[int]:
        """Every step of the pipeline (removed ones too), for listing its drafts."""
        pipeline = self._pipeline(user_id, pipeline_id, live=False, need="read")
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
        if self._sites is not None and pipeline.site_id is not None:
            site = self._sites.get_site(pipeline.site_id)
            if site is not None and site.deleted_at is not None:
                raise ConflictError("this pipeline's site is removed; restore the site first")
        try:
            restored = self._store.set_pipeline_deleted(pipeline_id, None)
        except NameTakenError as exc:
            raise ConflictError("this site has a newer pipeline; remove that one first") from exc
        self._event(pipeline_id, None, user_id, "pipeline.restored", {})
        return restored

    # -- for the sites service (services/sites.py ProjectRules) -------------------------------

    def project_hospitals(self, project_id: int) -> set[int]:
        return self._store.project_hospital_ids(project_id)

    def live_pipeline_of_site(self, site_id: int) -> int | None:
        site = self._sites.get_site(site_id) if self._sites is not None else None
        if site is None:
            return None
        return next(
            (p.id for p in self._store.pipelines_of_project(site.project_id) if p.site_id == site_id and p.deleted_at is None),
            None,
        )

    def step_site(self, step_id: int) -> int | None:
        step = self._store.get_step(step_id)
        pipeline = self._store.get_pipeline(step.pipeline_id) if step is not None else None
        return pipeline.site_id if pipeline is not None else None

    def site_labels(self, site_ids: list[int]) -> dict[int, tuple[str, int]]:
        """site -> (label, hospital id), for the calendar (the caller checked the projects)."""
        if self._sites is None or not site_ids:
            return {}
        return {s.id: (s.label, s.hospital_id) for s in self._sites.sites(sorted(set(site_ids)))}

    # -- for the tasks service (services/tasks.py TaskRules) ----------------------------------

    def site_project(self, site_id: int) -> int | None:
        site = self._sites.get_site(site_id) if self._sites is not None else None
        return site.project_id if site is not None and site.deleted_at is None else None

    def step_project(self, step_id: int) -> int | None:
        step = self._store.get_step(step_id)
        if step is None or step.deleted_at is not None:
            return None
        pipeline = self._store.get_pipeline(step.pipeline_id)
        return pipeline.project_id if pipeline is not None else None

    # -- steps --------------------------------------------------------------------------------

    def add_step(
        self, user_id: str, pipeline_id: int, name: str | None, step_type_id: int | None,
        parent_step_id: int | None = None, subject: str = "",
    ) -> Step:
        """A main step, or a follow-up of `parent_step_id` (phase B2b). A follow-up of a
        follow-up answers the same main step (one level). Follow-up types need a parent, and a
        parent takes a follow-up type or a free name. Numbered types get the next number: live
        main steps of the type in the pipeline, or live follow-ups of the type under the parent."""
        pipeline = self._pipeline(user_id, pipeline_id)
        steps = self._store.steps([pipeline.id])
        live = [s for s in steps if s.deleted_at is None]
        if len(live) >= MAX_STEPS:
            raise ConflictError(f"a pipeline may have at most {MAX_STEPS} steps")
        parent = None
        if parent_step_id is not None:
            parent = next((s for s in live if s.id == parent_step_id), None)
            if parent is None:
                raise NotFoundError("the step to answer is not a live step of this pipeline")
            if parent.parent_step_id is not None:  # answer to a follow-up: the same main step
                parent = next((s for s in live if s.id == parent.parent_step_id), None)
                if parent is None:
                    raise ConflictError("the main step of that follow-up is removed; restore it first")
        stype = None
        if step_type_id is not None:
            stype = next((t for t in self._setup.step_types() if t.id == step_type_id), None)
            if stype is None:
                raise ValidationError("step type not found or archived")
            if stype.category == FOLLOW_UP and parent is None:
                raise ValidationError("a follow-up step needs the step it answers")
            if parent is not None and stype.category != FOLLOW_UP:
                raise ValidationError("a follow-up step takes a follow-up type or a free name")
            name = _clean(name or stype.name, MAX_STEP_NAME, "step name")
        else:
            name = _clean(name or "", MAX_STEP_NAME, "step name")
        values: dict = {
            "step_type_id": step_type_id, "name": name,
            "position": max((s.position for s in steps), default=0) + 1,
            "status_key": self._first_open_status(),
            "subject": _subject(subject),
        }
        if parent is not None:
            values["parent_step_id"] = parent.id
        if stype is not None and stype.numbered:
            # The next number after the highest live one (not a count: a removed 1차 or a hand-set
            # 3차 must not give a second 2차).
            siblings = [s for s in live if s.step_type_id == stype.id and s.parent_step_id == (parent.id if parent else None)]
            values["sequence_no"] = min(max((s.sequence_no or 0 for s in siblings), default=0) + 1, MAX_SEQUENCE)
        slots = slots_from(self._setup.document_kinds_for([stype.id], pipeline.hospital_id)[stype.id]) if stype else []
        try:
            step = self._store.add_step(pipeline.id, values, slots)
        except StepParentError as exc:
            raise ConflictError(f"{exc}; reload the page") from exc
        self._event(
            pipeline.id, step.id, user_id, "step.added",
            {
                "name": name, "step_type_id": step_type_id, "parent_step_id": values.get("parent_step_id"),
                "sequence_no": values.get("sequence_no"),
            },
        )
        return step

    def update_step(self, user_id: str, pipeline_id: int, step_id: int, changes: dict) -> Step:
        """`changes` holds only the fields sent: name, status_key, due_on, submitted_on,
        decided_on, note (a date of None or "" clears it), from phase B2b subject,
        sequence_no (None clears it) and result_key (None clears it), and from CAL-1
        target_session_id (a session of the pipeline's hospital; None clears it). Setting a result fills
        the decision date when it is empty and not sent; the status is never changed by it
        (the page offers one, owner 2026-11-14)."""
        pipeline = self._pipeline(user_id, pipeline_id)
        step = self._step(pipeline, step_id)
        values: dict = {}
        if "name" in changes:
            values["name"] = _clean(changes["name"] or "", MAX_STEP_NAME, "step name")
        if "subject" in changes:
            values["subject"] = _subject(changes["subject"] or "")
        if "sequence_no" in changes:
            number = changes["sequence_no"]
            if number is not None and not (isinstance(number, int) and 1 <= number <= MAX_SEQUENCE):
                raise ValidationError(f"the number must be 1 to {MAX_SEQUENCE}, or empty")
            values["sequence_no"] = number
        if "result_key" in changes and changes["result_key"] != step.result_key:
            key = changes["result_key"] or None
            if key is not None:
                result = next((r for r in self._setup.results() if r.key == key), None)
                if result is None:
                    raise ValidationError("unknown or archived result")
                if "decided_on" not in changes and not step.decided_on:
                    values["decided_on"] = self._today()
            values["result_key"] = key
        if "target_session_id" in changes and changes["target_session_id"] != step.target_session_id:
            target = changes["target_session_id"]
            if target is not None:
                if self._sessions is None:
                    raise ConflictError("IRB sessions are not set up on this server")
                picked = self._sessions.check_target(int(target), pipeline.hospital_id)
                # The due date follows the meeting's cutoff (else its day) when the user did not
                # send one and it is empty or was filled from the earlier target (owner 2026-11-18);
                # a date the user typed is kept.
                # A due date sent back unchanged (a form that sends every field) counts as not sent.
                # The decision date (IRB 심의일) follows the meeting's day by the same rule.
                for field_name, day in (("due_on", picked.deadline), ("decided_on", picked.meets_on)):
                    sent = field_name in changes and (changes[field_name] or None) != (getattr(step, field_name) or None)
                    if not sent and self._follows_target(step, field_name):
                        changes = {k: v for k, v in changes.items() if k != field_name}  # the echoed value
                        values[field_name] = day
            elif "decided_on" not in changes and step.decided_on:
                # Unpicking: a decision date that was the meeting's day goes with it.
                previous, failed = self._target_session(step)
                if not failed and previous is not None and step.decided_on == previous.meets_on:
                    values["decided_on"] = None
            values["target_session_id"] = target
        if changes.get("custom_dates") is not None:
            values["custom_dates"] = _custom_dates(changes["custom_dates"])
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
                values.setdefault("decided_on", self._today())
        values = {k: v for k, v in values.items() if getattr(step, k) != v}
        if not values:
            return step
        updated = self._update_step(step.id, {**values, "updated_at": self._now()})
        payload = {k: {"from": getattr(step, k), "to": v} for k, v in values.items() if k not in ("note", "custom_dates")}
        if "note" in values:
            payload["note"] = True
        if "custom_dates" in values:  # the names stay out of the audit trail: only how many
            payload["custom_dates"] = {"from": len(step.custom_dates), "to": len(values["custom_dates"])}
        self._event(pipeline.id, step.id, user_id, "step.updated", payload)
        return updated

    def reorder_steps(self, user_id: str, pipeline_id: int, step_ids: list[int]) -> list[Step]:
        """Every live step once. Main steps take the order sent; each keeps its follow-ups right
        after it, in their sent order (a follow-up never moves away from its main step)."""
        pipeline = self._pipeline(user_id, pipeline_id)
        live = [s for s in self._store.steps([pipeline.id]) if s.deleted_at is None]
        if sorted(step_ids) != sorted(s.id for s in live) or len(set(step_ids)) != len(step_ids):
            raise ValidationError("send every step of the pipeline exactly once")
        by_id = {s.id: s for s in live}
        order = [s.id for group in _groups([by_id[sid] for sid in step_ids]) for s in group]
        try:
            # One database call: the whole order is saved or nothing is.
            self._store.reorder_steps(pipeline.id, order)
        except StepListError as exc:
            raise ConflictError("the steps changed meanwhile; reload the page and try again") from exc
        self._event(pipeline.id, None, user_id, "step.reordered", {"step_ids": order})
        return [s for s in display_order(self._store.steps([pipeline.id])) if s.deleted_at is None]

    def remove_step(self, user_id: str, pipeline_id: int, step_id: int) -> Step:
        pipeline = self._pipeline(user_id, pipeline_id)
        step = self._step(pipeline, step_id)
        answers = [
            s for s in self._store.steps([pipeline.id]) if s.parent_step_id == step.id and s.deleted_at is None
        ]
        if answers:
            raise ConflictError(
                f"this step has {len(answers)} follow-up step(s); remove them first: "
                + ", ".join(s.label for s in answers)
            )
        removed = self._update_step(step.id, {"deleted_at": self._now()})
        self._event(pipeline.id, step.id, user_id, "step.removed", {"name": step.name})
        return removed

    def restore_step(self, user_id: str, pipeline_id: int, step_id: int) -> Step:
        pipeline = self._pipeline(user_id, pipeline_id)
        step = self._step(pipeline, step_id, live=False)
        if step.deleted_at is None:
            raise ConflictError("this step is not removed")
        all_steps = self._store.steps([pipeline.id])
        if step.parent_step_id is not None:
            parent = next((s for s in all_steps if s.id == step.parent_step_id), None)
            if parent is not None and parent.deleted_at is not None:
                raise ConflictError(f"the step it answers ({parent.label}) is removed; restore that one first")
        live = [s for s in all_steps if s.deleted_at is None]
        if len(live) >= MAX_STEPS:
            raise ConflictError(f"a pipeline may have at most {MAX_STEPS} steps; remove one first")
        position = max((s.position for s in live), default=0) + 1
        try:
            restored = self._update_step(step.id, {"deleted_at": None, "position": position})
        except NameTakenError as exc:  # pipeline_steps_live_copy_key (phase C2)
            raise ConflictError(
                "another live step of this pipeline is a copy of the same step; remove that one first"
            ) from exc
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
        self, user_id: str, pipeline_id: int, step_id: int, name: str, filename: str, content: bytes,
        document_id: int | None = None, section: str | None = None,
    ) -> tuple[StepDocument, DocVersion]:
        """Into a slot (`document_id`), or by name: the live slot of that name, else a new slot
        of the user's in `section` (default 제출서류). A new file sets the slot in progress,
        also a final one (phase C1: the user marks the newest file final again)."""
        pipeline = self._pipeline(user_id, pipeline_id)
        step = self._step(pipeline, step_id)
        docs = self._store.documents([step.id])  # read once for the whole upload
        if document_id is not None:
            doc = next((d for d in docs if d.id == document_id), None)
            if doc is None:
                raise NotFoundError("document not found")
            if doc.deleted_at is not None:
                raise ConflictError("this document is removed; restore it first")
            name = doc.name
        else:
            name = _clean(name, MAX_DOC_NAME, "document name")
            doc = next((d for d in docs if d.deleted_at is None and d.name.lower() == name.lower()), None)
            section = section or "submission"
            if doc is None:  # the section is used only for a new slot
                if section not in SECTIONS:
                    raise ValidationError(f"the section must be one of {', '.join(SECTIONS)}")
                if sum(1 for d in docs if d.deleted_at is None) >= MAX_DOCUMENTS:
                    raise ConflictError(f"a step may have at most {MAX_DOCUMENTS} documents")
        filename = filename.strip().replace("\\", "/").rsplit("/", 1)[-1]
        if not filename or len(filename) > 255:
            raise ValidationError("the file needs a name of at most 255 characters")
        if not content:
            raise ValidationError("the file is empty")
        if len(content) > self._max_bytes:
            raise ValidationError(f"the file is larger than {self._max_bytes // 1_000_000} MB")
        sha = hashlib.sha256(content).hexdigest()
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
                    doc = created = self._store.create_document(
                        step.id, name,
                        {"section": section, "status": IN_PROGRESS, "position": self._next_position(docs, section or "submission")},
                    )
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
        payload = {"document_id": doc.id, "name": doc.name, "version_no": version.version_no, "filename": filename}
        status = doc.status
        if doc.status != IN_PROGRESS:
            try:
                self._store.update_document(doc.id, {"status": IN_PROGRESS, "updated_at": self._now()})
                payload["status"] = {"from": doc.status, "to": IN_PROGRESS}
                status = IN_PROGRESS
            except Exception:
                # The file is saved; a status left behind is shown as it is, and the next
                # upload or "Mark final" fixes it.
                log.exception("the status of document %s could not be set after an upload", doc.id)
        self._event(pipeline.id, step.id, user_id, "document.uploaded", payload)
        # The answer includes the version just added (newest first), without reading again.
        return replace(doc, versions=(version, *doc.versions), status=status), version

    @staticmethod
    def _next_position(docs: list[StepDocument], section: str) -> int:
        return max((d.position for d in docs if d.section == section), default=0) + 1

    def add_document(self, user_id: str, pipeline_id: int, step_id: int, name: str, section: str = "submission") -> StepDocument:
        """A slot of the user's own, without a file (phase C1): not required."""
        pipeline = self._pipeline(user_id, pipeline_id)
        step = self._step(pipeline, step_id)
        name = _clean(name, MAX_DOC_NAME, "document name")
        if section not in SECTIONS:
            raise ValidationError(f"the section must be one of {', '.join(SECTIONS)}")
        docs = self._store.documents([step.id])
        if sum(1 for d in docs if d.deleted_at is None) >= MAX_DOCUMENTS:
            raise ConflictError(f"a step may have at most {MAX_DOCUMENTS} documents")
        try:
            doc = self._store.create_document(
                step.id, name, {"section": section, "status": NEEDED, "position": self._next_position(docs, section)},
            )
        except NameTakenError as exc:
            raise ConflictError("a document with this name exists in the step") from exc
        self._event(pipeline.id, step.id, user_id, "document.added", {"document_id": doc.id, "name": name, "section": section})
        return doc

    def update_document(self, user_id: str, pipeline_id: int, step_id: int, document_id: int, changes: dict) -> StepDocument:
        """`changes` holds the fields sent: name and section (a user's own slot only; a listed
        document keeps the list's), note, status. Status: final (needs a file), not_needed, or
        needed / in_progress to reopen it (the server picks: in progress when it has a file)."""
        pipeline = self._pipeline(user_id, pipeline_id)
        step = self._step(pipeline, step_id)
        doc = self._document(step, document_id)
        values: dict = {}
        if ("name" in changes or "section" in changes) and doc.listed:
            raise ValidationError("a listed document keeps its name and section; add your own document instead")
        if "name" in changes:
            name = _clean(changes["name"] or "", MAX_DOC_NAME, "document name")
            if name != doc.name:
                values["name"] = name
        if "section" in changes:
            if changes["section"] not in SECTIONS:
                raise ValidationError(f"the section must be one of {', '.join(SECTIONS)}")
            if changes["section"] != doc.section:
                values["section"] = changes["section"]
                values["position"] = self._next_position(self._store.documents([step.id]), changes["section"])
        if "note" in changes:
            note = (changes["note"] or "").strip()
            if len(note) > MAX_DOC_NOTE:
                raise ValidationError(f"the note may have at most {MAX_DOC_NOTE:,} characters")
            if note != doc.note:
                values["note"] = note
        if "status" in changes:
            status = changes["status"]
            if status not in SLOT_STATUSES:
                raise ValidationError(f"the status must be one of {', '.join(SLOT_STATUSES)}")
            if status == FINAL and not doc.versions:
                raise ValidationError("upload a file before marking the document final")
            if status in (NEEDED, IN_PROGRESS):
                status = IN_PROGRESS if doc.versions else NEEDED
            if status != doc.status:
                values["status"] = status
        if not values:
            return doc
        try:
            updated = self._store.update_document(doc.id, {**values, "updated_at": self._now()})
        except NameTakenError as exc:
            raise ConflictError("a document with this name exists in the step") from exc
        except LookupError as exc:
            raise NotFoundError("document not found") from exc
        payload: dict = {"document_id": doc.id, "name": updated.name}
        for k, v in values.items():
            if k == "note":
                payload["note"] = True  # the text stays out of the history, as for steps
            elif k != "position":
                payload[k] = {"from": getattr(doc, k), "to": v}
        self._event(pipeline.id, step.id, user_id, "document.updated", payload)
        return replace(updated, versions=doc.versions)

    def reorder_documents(self, user_id: str, pipeline_id: int, step_id: int, document_ids: list[int]) -> list[StepDocument]:
        """Every live slot of the step, each once, in the new order (positions across sections;
        the page groups them by section)."""
        pipeline = self._pipeline(user_id, pipeline_id)
        step = self._step(pipeline, step_id)
        try:
            self._store.reorder_documents(step.id, document_ids)
        except StepListError as exc:
            raise ConflictError("the documents changed meanwhile; reload the page") from exc
        self._event(pipeline.id, step.id, user_id, "documents.reordered", {"document_ids": document_ids})
        return slot_order(self._store.documents([step.id]))

    def _drop(self, paths: list[str]) -> None:
        try:
            self._files.delete(paths)
        except Exception:
            log.exception("could not delete pipeline files %s", paths)

    def remove_document(self, user_id: str, pipeline_id: int, step_id: int, document_id: int) -> StepDocument:
        pipeline = self._pipeline(user_id, pipeline_id)
        step = self._step(pipeline, step_id)
        doc = self._document(step, document_id)
        if doc.listed:  # owner 2026-11-15: a listed document stays; the checklist must not lose it
            raise ValidationError("a listed document cannot be removed; mark it not needed instead")
        removed = self._store.set_document_deleted(doc.id, self._now())
        self._event(pipeline.id, step.id, user_id, "document.removed", {"document_id": doc.id, "name": doc.name})
        return removed

    def restore_document(self, user_id: str, pipeline_id: int, step_id: int, document_id: int) -> StepDocument:
        pipeline = self._pipeline(user_id, pipeline_id)
        step = self._step(pipeline, step_id)
        docs = self._store.documents([step.id])
        doc = next((d for d in docs if d.id == document_id), None)
        if doc is None:
            raise NotFoundError("document not found")
        if doc.deleted_at is None:
            raise ConflictError("this document is not removed")
        if sum(1 for d in docs if d.deleted_at is None) >= MAX_DOCUMENTS:
            raise ConflictError(f"a step may have at most {MAX_DOCUMENTS} documents; remove one first")
        try:
            restored = self._store.set_document_deleted(doc.id, None)
        except NameTakenError as exc:
            raise ConflictError("a document with this name exists in the step; rename or remove it first") from exc
        self._event(pipeline.id, step.id, user_id, "document.restored", {"document_id": doc.id, "name": doc.name})
        return restored

    def download_url(self, user_id: str, pipeline_id: int, step_id: int, document_id: int, version_no: int) -> str:
        pipeline = self._pipeline(user_id, pipeline_id, live=False, need="read")
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
        pipeline = self._pipeline(user_id, pipeline_id, live=False, need="read")
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
