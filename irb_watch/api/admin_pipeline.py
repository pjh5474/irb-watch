"""Admin routes for the IRB pipeline setup: statuses, step types, default step order."""

from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from irb_watch.api.deps import get_pipeline_setup_service, require_admin
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.pipeline_admin import (
    MAX_DESCRIPTION_CHARS,
    MAX_INSTRUCTIONS_CHARS,
    MAX_LABEL_CHARS,
    MAX_NAME_CHARS,
    PipelineSetupService,
    PipelineTemplate,
    StepResult,
    StepStatus,
    StepType,
    StepTypeEdit,
)

router = APIRouter(prefix="/admin/pipeline", tags=["admin-pipeline"])


class StatusOut(BaseModel):
    key: str
    label: str
    closed: bool
    position: int
    archived_at: str | None

    @classmethod
    def of(cls, s: StepStatus) -> "StatusOut":
        return cls(**s.__dict__)


class StatusIn(BaseModel):
    label: str = Field(min_length=1, max_length=MAX_LABEL_CHARS)
    closed: bool = False


class StatusPatch(BaseModel):
    label: str | None = Field(None, min_length=1, max_length=MAX_LABEL_CHARS)
    closed: bool | None = None


class StatusOrderIn(BaseModel):
    keys: list[str] = Field(max_length=100)


class ResultOut(BaseModel):
    key: str
    label: str
    approved: bool
    needs_answer: bool
    position: int
    archived_at: str | None

    @classmethod
    def of(cls, r: StepResult) -> "ResultOut":
        return cls(**r.__dict__)


class ResultIn(BaseModel):
    label: str = Field(min_length=1, max_length=MAX_LABEL_CHARS)
    approved: bool = False
    needs_answer: bool = False


class ResultPatch(BaseModel):
    label: str | None = Field(None, min_length=1, max_length=MAX_LABEL_CHARS)
    approved: bool | None = None
    needs_answer: bool | None = None


class StepTypeOut(BaseModel):
    id: int
    name: str
    description: str
    instructions: str
    guide_ids: list[int]
    form_template_id: int | None
    input_earlier_steps: bool
    input_board_documents: bool
    required_documents: list[str]
    archived_at: str | None
    updated_at: str
    category: str
    numbered: bool

    @classmethod
    def of(cls, t: StepType) -> "StepTypeOut":
        return cls(
            **{**t.__dict__, "guide_ids": list(t.guide_ids), "required_documents": list(t.required_documents)}
        )


class StepTypeIn(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    description: str = Field("", max_length=MAX_DESCRIPTION_CHARS)
    instructions: str = Field("", max_length=MAX_INSTRUCTIONS_CHARS)
    guide_ids: list[int] = Field(default_factory=list, max_length=50)
    form_template_id: int | None = None
    input_earlier_steps: bool = True
    input_board_documents: bool = False
    required_documents: list[str] = Field(default_factory=list, max_length=100)
    # One list of categories: the service checks it (services.pipeline_admin.CATEGORIES, 422).
    category: str = Field("other", max_length=40)
    numbered: bool = False

    def edit(self) -> StepTypeEdit:
        return StepTypeEdit(**self.model_dump())


class TemplateOut(BaseModel):
    hospital_id: int | None
    step_type_ids: list[int]
    updated_at: str

    @classmethod
    def of(cls, t: PipelineTemplate) -> "TemplateOut":
        return cls(hospital_id=t.hospital_id, step_type_ids=list(t.step_type_ids), updated_at=t.updated_at)


class TemplateIn(BaseModel):
    step_type_ids: list[int] = Field(max_length=100)


Service = PipelineSetupService


# -- statuses -----------------------------------------------------------------------------------


@router.get("/statuses", response_model=list[StatusOut])
def list_statuses(
    include_archived: bool = False,
    _: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> list[StatusOut]:
    return [StatusOut.of(s) for s in service.statuses(include_archived)]


@router.post("/statuses", status_code=201, response_model=StatusOut)
def add_status(
    body: StatusIn,
    admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> StatusOut:
    return StatusOut.of(service.add_status(admin.id, body.label, body.closed))


@router.put("/statuses/order", response_model=list[StatusOut])
def reorder_statuses(
    body: StatusOrderIn,
    admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> list[StatusOut]:
    return [StatusOut.of(s) for s in service.reorder_statuses(admin.id, body.keys)]


@router.patch("/statuses/{key}", response_model=StatusOut)
def update_status(
    key: str,
    body: StatusPatch,
    admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> StatusOut:
    return StatusOut.of(service.update_status(admin.id, key, body.label, body.closed))


@router.post("/statuses/{key}/archive", response_model=StatusOut)
def archive_status(
    key: str,
    admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> StatusOut:
    return StatusOut.of(service.archive_status(admin.id, key))


@router.post("/statuses/{key}/restore", response_model=StatusOut)
def restore_status(
    key: str,
    admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> StatusOut:
    return StatusOut.of(service.restore_status(admin.id, key))


# -- IRB results (phase B2b) --------------------------------------------------------------------


@router.get("/results", response_model=list[ResultOut])
def list_results(
    include_archived: bool = False,
    _: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> list[ResultOut]:
    return [ResultOut.of(r) for r in service.results(include_archived)]


@router.post("/results", status_code=201, response_model=ResultOut)
def add_result(
    body: ResultIn,
    admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> ResultOut:
    return ResultOut.of(service.add_result(admin.id, body.label, body.approved, body.needs_answer))


@router.put("/results/order", response_model=list[ResultOut])
def reorder_results(
    body: StatusOrderIn,
    admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> list[ResultOut]:
    return [ResultOut.of(r) for r in service.reorder_results(admin.id, body.keys)]


@router.patch("/results/{key}", response_model=ResultOut)
def update_result(
    key: str,
    body: ResultPatch,
    admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> ResultOut:
    return ResultOut.of(service.update_result(admin.id, key, body.label, body.approved, body.needs_answer))


@router.post("/results/{key}/{action}", response_model=ResultOut)
def result_archived(
    key: str,
    action: Literal["archive", "restore"],
    admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> ResultOut:
    if action == "archive":
        return ResultOut.of(service.archive_result(admin.id, key))
    return ResultOut.of(service.restore_result(admin.id, key))


# -- step types ---------------------------------------------------------------------------------


@router.get("/step-types", response_model=list[StepTypeOut])
def list_step_types(
    include_archived: bool = False,
    _: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> list[StepTypeOut]:
    return [StepTypeOut.of(t) for t in service.step_types(include_archived)]


@router.post("/step-types", status_code=201, response_model=StepTypeOut)
def create_step_type(
    body: StepTypeIn,
    admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> StepTypeOut:
    return StepTypeOut.of(service.create_step_type(admin.id, body.edit()))


@router.put("/step-types/{type_id}", response_model=StepTypeOut)
def update_step_type(
    type_id: int,
    body: StepTypeIn,
    admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> StepTypeOut:
    return StepTypeOut.of(service.update_step_type(admin.id, type_id, body.edit()))


@router.post("/step-types/{type_id}/archive", response_model=StepTypeOut)
def archive_step_type(
    type_id: int,
    admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> StepTypeOut:
    return StepTypeOut.of(service.archive_step_type(admin.id, type_id))


@router.post("/step-types/{type_id}/restore", response_model=StepTypeOut)
def restore_step_type(
    type_id: int,
    admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> StepTypeOut:
    return StepTypeOut.of(service.restore_step_type(admin.id, type_id))


# -- default step order -------------------------------------------------------------------------


@router.get("/templates", response_model=list[TemplateOut])
def list_templates(
    _: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> list[TemplateOut]:
    return [TemplateOut.of(t) for t in service.templates()]


@router.put("/templates/global", response_model=TemplateOut)
def save_global_template(
    body: TemplateIn,
    admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> TemplateOut:
    return TemplateOut.of(service.save_template(admin.id, None, body.step_type_ids))


@router.put("/templates/hospitals/{hospital_id}", response_model=TemplateOut)
def save_hospital_template(
    hospital_id: int,
    body: TemplateIn,
    admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> TemplateOut:
    return TemplateOut.of(service.save_template(admin.id, hospital_id, body.step_type_ids))


@router.delete("/templates/hospitals/{hospital_id}", status_code=204)
def delete_hospital_template(
    hospital_id: int,
    admin: AuthUser = Depends(require_admin),
    service: Service = Depends(get_pipeline_setup_service),
) -> Response:
    service.delete_template(admin.id, hospital_id)
    return Response(status_code=204)
