"""Users' IRB pipelines (owner only): pipelines, steps, step documents and history."""

import base64
import binascii
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from irb_watch.api.deps import current_user, get_pipeline_service
from irb_watch.auth.jwt import AuthUser
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
)

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

    @classmethod
    def of(cls, s: PipelineSummary) -> "PipelineOut":
        p = s.pipeline
        return cls(
            id=p.id, project_id=p.project_id, hospital_id=p.hospital_id, hospital_name=s.hospital_name,
            in_project=s.in_project, steps=s.steps, closed_steps=s.closed_steps,
            created_at=p.created_at, updated_at=p.updated_at, deleted_at=p.deleted_at,
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
    documents: list[DocumentOut] = []
    checklist: list[dict] = []

    @classmethod
    def of(cls, s: Step, type_name: str | None = None, docs=(), checklist=()) -> "StepOut":
        return cls(
            id=s.id, step_type_id=s.step_type_id, step_type_name=type_name, name=s.name,
            position=s.position, status_key=s.status_key, due_on=s.due_on, submitted_on=s.submitted_on,
            decided_on=s.decided_on, note=s.note, deleted_at=s.deleted_at,
            documents=[DocumentOut.of(d) for d in docs],
            checklist=[{"name": c.name, "uploaded": c.uploaded} for c in checklist],
        )


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
    pipeline: PipelineOut
    steps: list[StepOut]
    statuses: list[StatusOut]
    step_types: list[StepTypeOut]

    @classmethod
    def of(cls, d: PipelineDetail) -> "DetailOut":
        return cls(
            pipeline=PipelineOut.of(d.summary),
            steps=[StepOut.of(v.step, v.step_type_name, v.documents, v.checklist) for v in d.steps],
            statuses=[StatusOut.of(s) for s in d.statuses],
            step_types=[StepTypeOut.of(t) for t in d.step_types],
        )


class StartIn(BaseModel):
    hospital_id: int


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

    @classmethod
    def of(cls, e: Event, user_id: str) -> "EventOut":
        return cls(
            id=e.id, step_id=e.step_id, mine=e.actor_id == user_id, event_type=e.event_type,
            payload=e.payload, created_at=e.created_at,
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
    return PipelineRef.of(service.start(user.id, project_id, body.hospital_id))


@router.get("/pipelines/{pipeline_id}", response_model=DetailOut)
def pipeline_detail(
    pipeline_id: int,
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
) -> DetailOut:
    return DetailOut.of(service.detail(user.id, pipeline_id))


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
    user: AuthUser = Depends(current_user),
    service: Service = Depends(get_pipeline_service),
) -> list[EventOut]:
    return [EventOut.of(e, user.id) for e in service.history(user.id, pipeline_id, limit)]


# -- steps --------------------------------------------------------------------------------------


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
