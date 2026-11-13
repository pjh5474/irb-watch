"""Users' IRB pipelines (owner only): pipelines, steps, step documents and history."""

import base64
import binascii
from collections import Counter
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from irb_watch.api.deps import (
    current_user,
    get_docgen_service,
    get_draft_job_service,
    get_pipeline_service,
    get_project_access_service,
)
from irb_watch.api.docgen import DraftOut, JobOut
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.docgen import DocgenService
from irb_watch.services.draft_jobs import DraftJobService
from irb_watch.services.draft_jobs import job_step as _job_step
from irb_watch.services.errors import ValidationError
from irb_watch.services.pipeline_admin import StepStatus, StepType
from irb_watch.services.pipelines import (
    MAX_DOC_NAME,
    MAX_NOTE,
    MAX_STEP_NAME,
    DocVersion,
    Event,
    Pipeline,
    PipelineDetail,
    PipelineService,
    PipelineSummary,
    Step,
    StepDocument,
    StepView,
)
from irb_watch.services.project_access import ProjectAccessService

router = APIRouter(tags=["pipelines"])

Service = PipelineService


class PipelineOut(BaseModel):
    id: int
    project_id: int
    hospital_id: int
    hospital_name: str | None
    in_project: bool
    steps: int
    closed_steps: int
    created_at: str
    updated_at: str
    deleted_at: str | None
    site_id: int | None = None  # phase B2a: the site (hospital + PI) of this pipeline
    pi_name: str = ""
    site_no: str = ""

    @classmethod
    def of(cls, s: PipelineSummary) -> "PipelineOut":
        p = s.pipeline
        return cls(
            id=p.id, project_id=p.project_id, hospital_id=p.hospital_id, hospital_name=s.hospital_name,
            in_project=s.in_project, steps=s.steps, closed_steps=s.closed_steps,
            created_at=p.created_at, updated_at=p.updated_at, deleted_at=p.deleted_at,
            site_id=p.site_id, pi_name=s.site.pi_name if s.site else "", site_no=s.site.site_no if s.site else "",
        )


class PipelineRef(BaseModel):
    id: int
    project_id: int
    hospital_id: int
    deleted_at: str | None

    @classmethod
    def of(cls, p: Pipeline) -> "PipelineRef":
        return cls(id=p.id, project_id=p.project_id, hospital_id=p.hospital_id, deleted_at=p.deleted_at)


class VersionOut(BaseModel):
    version_no: int
    filename: str
    size_bytes: int
    has_text: bool
    text_note: str | None
    created_at: str

    @classmethod
    def of(cls, v: DocVersion) -> "VersionOut":
        return cls(
            version_no=v.version_no, filename=v.filename, size_bytes=v.size_bytes,
            has_text=v.has_text, text_note=v.text_note, created_at=v.created_at,
        )


class DocumentOut(BaseModel):
    id: int
    name: str
    created_at: str
    deleted_at: str | None
    versions: list[VersionOut]

    @classmethod
    def of(cls, d: StepDocument) -> "DocumentOut":
        return cls(
            id=d.id, name=d.name, created_at=d.created_at, deleted_at=d.deleted_at,
            versions=[VersionOut.of(v) for v in d.versions],
        )


class StepOut(BaseModel):
    id: int
    step_type_id: int | None
    step_type_name: str | None
    name: str
    position: int
    status_key: str
    due_on: str | None
    submitted_on: str | None
    decided_on: str | None
    note: str
    deleted_at: str | None

    @classmethod
    def of(cls, s: Step, type_name: str | None = None) -> "StepOut":
        return cls(
            id=s.id, step_type_id=s.step_type_id, step_type_name=type_name, name=s.name,
            position=s.position, status_key=s.status_key, due_on=s.due_on, submitted_on=s.submitted_on,
            decided_on=s.decided_on, note=s.note, deleted_at=s.deleted_at,
        )


class OverviewStepOut(StepOut):
    """A step row of the overview: counts only, no documents (they are on the step page)."""

    documents: int  # live documents with at least one file
    required: int  # checklist names of the step type
    required_missing: int
    drafts: int  # the user's drafts made in this step
    open_jobs: int  # draft jobs queued or running for this step

    @classmethod
    def of_view(cls, v: StepView, drafts: int, open_jobs: int) -> "OverviewStepOut":
        return cls(
            **StepOut.of(v.step, v.step_type_name).model_dump(),
            documents=v.documents_with_files, required=len(v.checklist),
            required_missing=v.required_missing, drafts=drafts, open_jobs=open_jobs,
        )


class ChecklistOut(BaseModel):
    name: str
    uploaded: bool


class StatusOut(BaseModel):
    key: str
    label: str
    closed: bool
    archived: bool

    @classmethod
    def of(cls, s: StepStatus) -> "StatusOut":
        return cls(key=s.key, label=s.label, closed=s.closed, archived=s.archived_at is not None)


class StepTypeOut(BaseModel):
    id: int
    name: str
    description: str

    @classmethod
    def of(cls, t: StepType) -> "StepTypeOut":
        return cls(id=t.id, name=t.name, description=t.description)


class DetailOut(BaseModel):
    """The pipeline overview (phase A): steps with counts; documents are on each step page."""

    pipeline: PipelineOut
    steps: list[OverviewStepOut]
    statuses: list[StatusOut]
    step_types: list[StepTypeOut]
    access: str  # owner | manage | pm | edit | read (read: change nothing)

    @classmethod
    def of(cls, d: PipelineDetail, drafts: Counter, open_jobs: Counter) -> "DetailOut":
        return cls(
            pipeline=PipelineOut.of(d.summary),
            steps=[OverviewStepOut.of_view(v, drafts[v.step.id], open_jobs[v.step.id]) for v in d.steps],
            statuses=[StatusOut.of(s) for s in d.statuses],
            step_types=[StepTypeOut.of(t) for t in d.step_types],
            access=d.access,
        )


class StepPageOut(BaseModel):
    """The step page in one call: the step, its documents with every version, the checklist,
    the step's drafts (everyone's: they belong to the project) and its open or recently failed
    draft jobs (everyone's)."""

    pipeline: PipelineOut
    step: StepOut
    documents: list[DocumentOut]
    checklist: list[ChecklistOut]
    drafts: list[DraftOut]
    jobs: list[JobOut]
    previous_step_id: int | None
    next_step_id: int | None
    statuses: list[StatusOut]
    access: str
    shared: bool  # the project has members: a draft made here is read by all of them


class StartIn(BaseModel):
    """site_id: start that site's pipeline. hospital_id alone: the hospital's unnamed site (made
    when the hospital has no site yet; refused when it has named PIs)."""

    hospital_id: int | None = None
    site_id: int | None = None


class NewStepIn(BaseModel):
    name: str | None = Field(None, max_length=MAX_STEP_NAME + 50)
    step_type_id: int | None = None


class StepPatch(BaseModel):
    name: str | None = Field(None, max_length=MAX_STEP_NAME + 50)
    status_key: str | None = Field(None, max_length=40)
    due_on: str | None = Field(None, max_length=10)
    submitted_on: str | None = Field(None, max_length=10)
    decided_on: str | None = Field(None, max_length=10)
    note: str | None = Field(None, max_length=MAX_NOTE + 100)


class OrderIn(BaseModel):
    step_ids: list[int] = Field(max_length=200)


class UploadFileIn(BaseModel):
    filename: str = Field(max_length=300)
    # 20 MB of file is about 27 million base64 characters. The request is refused by its
    # declared size before it is read (admin_manual.limit_upload_size); the real per-file limit
    # (MAX_FILE_BYTES) is in the service.
    content_base64: str = Field(max_length=28_000_000)


class UploadIn(BaseModel):
    name: str = Field(max_length=MAX_DOC_NAME + 50)
    file: UploadFileIn


class UploadOut(BaseModel):
    document: DocumentOut
    version_no: int


class LinkOut(BaseModel):
    url: str


class EventOut(BaseModel):
    id: int
    step_id: int | None
    mine: bool
    event_type: str
    payload: dict
    created_at: str
    actor_name: str | None = None  # who did it, when it was someone else

    @classmethod
    def of(cls, e: Event, user_id: str, names: dict[str, str] | None = None) -> "EventOut":
        mine = e.actor_id == user_id
        return cls(
            id=e.id, step_id=e.step_id, mine=mine, event_type=e.event_type,
            payload=e.payload, created_at=e.created_at,
            actor_name=None if mine or not e.actor_id else (names or {}).get(e.actor_id),
        )


def _decode(file: UploadFileIn) -> bytes:
    try:
        return base64.b64decode(file.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError(f"{file.filename}: the file content is not valid base64") from exc


# -- pipelines ----------------------------------------------------------------------------------


@router.get("/projects/{project_id}/pipelines", response_model=list[PipelineOut])
def list_pipelines(
    project_id: int,
    include_removed: bool = False,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
) -> list[PipelineOut]:
    return [PipelineOut.of(s) for s in service.list_for_project(user.id, project_id, include_removed)]


@router.post("/projects/{project_id}/pipelines", status_code=201, response_model=PipelineRef)
def start_pipeline(
    project_id: int,
    body: StartIn,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
) -> PipelineRef:
    return PipelineRef.of(service.start(user.id, project_id, body.hospital_id, body.site_id))


@router.get("/pipelines/{pipeline_id}", response_model=DetailOut)
def pipeline_detail(
    pipeline_id: int,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
    docgen: DocgenService = Depends(get_docgen_service),
    jobs: DraftJobService = Depends(get_draft_job_service),
) -> DetailOut:
    detail = service.detail(user.id, pipeline_id)  # first: a stranger gets 404 here
    step_ids = [v.step.id for v in detail.steps]
    step_drafts = docgen.step_drafts(user.id, step_ids)
    drafts = Counter(d.pipeline_step_id for d in step_drafts)
    draft_steps = {d.id: d.pipeline_step_id for d in step_drafts}
    here = set(step_ids)
    # Everyone's jobs: in a shared project a member sees that a colleague's draft is being written.
    open_jobs = Counter(
        s for j in jobs.for_steps(step_ids, list(draft_steps)) if j.open and (s := _job_step(j, draft_steps)) in here
    )
    return DetailOut.of(detail, drafts, open_jobs)


@router.delete("/pipelines/{pipeline_id}", response_model=PipelineRef)
def remove_pipeline(
    pipeline_id: int,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
) -> PipelineRef:
    return PipelineRef.of(service.remove(user.id, pipeline_id))


@router.post("/pipelines/{pipeline_id}/restore", response_model=PipelineRef)
def restore_pipeline(
    pipeline_id: int,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
) -> PipelineRef:
    return PipelineRef.of(service.restore(user.id, pipeline_id))


@router.get("/pipelines/{pipeline_id}/history", response_model=list[EventOut])
def pipeline_history(
    pipeline_id: int,
    limit: int = Query(200, ge=1, le=500),
    step_id: int | None = None,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
    access: ProjectAccessService = Depends(get_project_access_service),
) -> list[EventOut]:
    """Newest first; with step_id, only that step's events. Each names who did it (project
    members see each other's work, phase B1)."""
    events = service.history(user.id, pipeline_id, limit, step_id)  # first: access check
    names = access.names([e.actor_id for e in events if e.actor_id and e.actor_id != user.id])
    return [EventOut.of(e, user.id, names) for e in events]


# -- steps --------------------------------------------------------------------------------------


@router.get("/pipelines/{pipeline_id}/steps/{step_id}", response_model=StepPageOut)
def step_page(
    pipeline_id: int,
    step_id: int,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
    docgen: DocgenService = Depends(get_docgen_service),
    jobs: DraftJobService = Depends(get_draft_job_service),
    access: ProjectAccessService = Depends(get_project_access_service),
) -> StepPageOut:
    """One step for its own page. A removed step, or a step of a removed pipeline, still
    answers (the page shows it read-only); a stranger's or unknown id answers 404."""
    page = service.step_page(user.id, pipeline_id, step_id)  # first: access check
    v = page.view
    drafts = docgen.step_drafts(user.id, [step_id])
    draft_steps = {d.id: d.pipeline_step_id for d in drafts}
    # Draft jobs of this step and revisions of its drafts, by anyone: open, or failed with a reason.
    step_jobs = [j for j in jobs.for_steps([step_id], list(draft_steps)) if _job_step(j, draft_steps) == step_id]
    names = access.names([d.user_id for d in drafts] + [j.user_id for j in step_jobs])
    return StepPageOut(
        pipeline=PipelineOut.of(page.summary),
        step=StepOut.of(v.step, v.step_type_name),
        documents=[DocumentOut.of(d) for d in v.documents],
        checklist=[ChecklistOut(name=c.name, uploaded=c.uploaded) for c in v.checklist],
        drafts=[DraftOut.of(d, user.id, names) for d in drafts],
        jobs=[JobOut.of(j, user.id, names) for j in step_jobs],
        previous_step_id=page.previous_step_id,
        next_step_id=page.next_step_id,
        statuses=[StatusOut.of(s) for s in page.statuses],
        access=page.access,
        shared=access.shared(page.summary.pipeline.project_id),
    )


@router.post("/pipelines/{pipeline_id}/steps", status_code=201, response_model=StepOut)
def add_step(
    pipeline_id: int,
    body: NewStepIn,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
) -> StepOut:
    return StepOut.of(service.add_step(user.id, pipeline_id, body.name, body.step_type_id))


@router.put("/pipelines/{pipeline_id}/steps/order", response_model=list[StepOut])
def reorder_steps(
    pipeline_id: int,
    body: OrderIn,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
) -> list[StepOut]:
    return [StepOut.of(s) for s in service.reorder_steps(user.id, pipeline_id, body.step_ids)]


@router.patch("/pipelines/{pipeline_id}/steps/{step_id}", response_model=StepOut)
def update_step(
    pipeline_id: int,
    step_id: int,
    body: StepPatch,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
) -> StepOut:
    # Only the fields sent count; a date sent as null clears it.
    changes = {k: getattr(body, k) for k in body.model_fields_set}
    return StepOut.of(service.update_step(user.id, pipeline_id, step_id, changes))


class DraftFileOut(BaseModel):
    document_id: int
    name: str
    step_id: int
    step_name: str
    version_no: int
    filename: str
    has_text: bool
    text_note: str | None
    ticked: bool


class DraftInputsOut(BaseModel):
    step_id: int
    step_name: str
    hospital_id: int
    files: list[DraftFileOut]
    allow_board_documents: bool
    guide_ids: list[int]
    form_template_id: int | None
    has_instructions: bool
    site_details: list[str] = []  # names of the site details that go to the AI (D18)


@router.get("/pipelines/{pipeline_id}/steps/{step_id}/draft-inputs", response_model=DraftInputsOut)
def draft_inputs(
    pipeline_id: int,
    step_id: int,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
) -> DraftInputsOut:
    """What "Draft for this step" starts from: the user's own files of this step and the
    earlier ones (newest versions; ticked when they have readable text) and the step type's
    defaults. The draft itself is started with POST /docgen/steps/{step_id}/drafts."""
    d = service.draft_inputs(user.id, pipeline_id, step_id)
    return DraftInputsOut(
        step_id=d.step_id, step_name=d.step_name, hospital_id=d.hospital_id,
        files=[DraftFileOut(**vars(f)) for f in d.files],
        allow_board_documents=d.allow_board_documents, guide_ids=list(d.guide_ids),
        form_template_id=d.form_template_id, has_instructions=d.has_instructions,
        site_details=list(d.site_details),
    )


@router.delete("/pipelines/{pipeline_id}/steps/{step_id}", response_model=StepOut)
def remove_step(
    pipeline_id: int,
    step_id: int,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
) -> StepOut:
    return StepOut.of(service.remove_step(user.id, pipeline_id, step_id))


@router.post("/pipelines/{pipeline_id}/steps/{step_id}/restore", response_model=StepOut)
def restore_step(
    pipeline_id: int,
    step_id: int,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
) -> StepOut:
    return StepOut.of(service.restore_step(user.id, pipeline_id, step_id))


# -- documents ----------------------------------------------------------------------------------


@router.post("/pipelines/{pipeline_id}/steps/{step_id}/documents", status_code=201, response_model=UploadOut)
def upload_document(
    pipeline_id: int,
    step_id: int,
    body: UploadIn,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
) -> UploadOut:
    doc, version = service.upload(user.id, pipeline_id, step_id, body.name, body.file.filename, _decode(body.file))
    return UploadOut(document=DocumentOut.of(doc), version_no=version.version_no)


@router.post(
    "/pipelines/{pipeline_id}/steps/{step_id}/documents/{document_id}/versions/{version_no}/download-url",
    response_model=LinkOut,
)
def download_url(
    pipeline_id: int,
    step_id: int,
    document_id: int,
    version_no: int,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
) -> LinkOut:
    return LinkOut(url=service.download_url(user.id, pipeline_id, step_id, document_id, version_no))


@router.post(
    "/pipelines/{pipeline_id}/steps/{step_id}/documents/{document_id}/{action}",
    response_model=DocumentOut,
)
def document_removed(
    pipeline_id: int,
    step_id: int,
    document_id: int,
    action: Literal["remove", "restore"],
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
) -> DocumentOut:
    if action == "remove":
        return DocumentOut.of(service.remove_document(user.id, pipeline_id, step_id, document_id))
    return DocumentOut.of(service.restore_document(user.id, pipeline_id, step_id, document_id))
