"""Admin: the form-template library of the docs generator (blank hospital forms as .docx).

A new or replaced file is in 'review' until the admin saves its checked field list.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from irb_watch.api.deps import get_docgen_service, require_admin
from irb_watch.api.docgen import FileIn, FormFieldOut, TemplateFileIn, TemplateOut
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.docgen import (
    MAX_DOC_TYPE_CHARS,
    MAX_HINT_CHARS,
    MAX_TITLE_CHARS,
    DocgenService,
    FieldEdit,
    UploadedFile,
)

router = APIRouter(prefix="/admin", tags=["admin-templates"])


class TemplateIn(BaseModel):
    title: str = Field(max_length=MAX_TITLE_CHARS)
    doc_type: str = Field(default="", max_length=MAX_DOC_TYPE_CHARS)
    file: FileIn


class TemplatePatch(BaseModel):
    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)
    doc_type: str | None = Field(default=None, max_length=MAX_DOC_TYPE_CHARS)
    is_global: bool | None = None


class FieldEditIn(BaseModel):
    key: str = Field(max_length=400)
    label: str = Field(max_length=200)
    hint: str = Field(default="", max_length=MAX_HINT_CHARS + 100)
    multi: bool = False


class FieldsIn(BaseModel):
    fields: list[FieldEditIn] = Field(max_length=200)


class SourcesIn(BaseModel):
    source_ids: list[int] = Field(max_length=1000)


@router.get("/templates", response_model=list[TemplateOut])
def list_templates(
    include_archived: bool = False,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> list[TemplateOut]:
    return [TemplateOut.of(t) for t in service.list_templates(admin.id, include_archived)]


@router.post("/templates", status_code=201, response_model=TemplateOut)
def create_template(
    body: TemplateIn,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> TemplateOut:
    """Upload a blank form (.docx, at most 5 MB). Its fields are found and listed for review;
    it is offered to users only after its fields are saved."""
    t = service.create_template(
        admin.id, body.title, body.doc_type, UploadedFile(body.file.filename, body.file.content_base64)
    )
    return TemplateOut.of(t)


@router.get("/templates/{template_id}", response_model=TemplateOut)
def get_template(
    template_id: int,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> TemplateOut:
    return TemplateOut.of(service.template(admin.id, template_id))


@router.get("/templates/{template_id}/detected", response_model=list[FormFieldOut])
def detected_fields(
    template_id: int,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> list[FormFieldOut]:
    """Every field found in the current file, including ones removed in review."""
    return [FormFieldOut.of(f) for f in service.detected_fields(admin.id, template_id)]


@router.put("/templates/{template_id}/fields", response_model=TemplateOut)
def save_fields(
    template_id: int,
    body: FieldsIn,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> TemplateOut:
    """The checked fields (kept ones, in order, with label, note and 'several allowed'). The
    template becomes ready."""
    edits = [FieldEdit(f.key, f.label, f.hint, f.multi) for f in body.fields]
    return TemplateOut.of(service.save_template_fields(admin.id, template_id, edits))


@router.patch("/templates/{template_id}", response_model=TemplateOut)
def update_template(
    template_id: int,
    body: TemplatePatch,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> TemplateOut:
    """Rename, set the document type, make global or site-specific."""
    return TemplateOut.of(
        service.update_template(admin.id, template_id, body.title, body.doc_type, body.is_global)
    )


@router.put("/templates/{template_id}/file", response_model=TemplateOut)
def replace_template_file(
    template_id: int,
    body: TemplateFileIn,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> TemplateOut:
    """A new file; back to review. Drafts already made keep the old file."""
    t = service.replace_template_file(
        admin.id, template_id, UploadedFile(body.file.filename, body.file.content_base64)
    )
    return TemplateOut.of(t)


@router.put("/templates/{template_id}/sources", response_model=TemplateOut)
def set_template_sources(
    template_id: int,
    body: SourcesIn,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> TemplateOut:
    """The full list of sites that offer this form (replaces the current list)."""
    return TemplateOut.of(service.set_template_sources(admin.id, template_id, body.source_ids))


@router.delete("/templates/{template_id}", status_code=204)
def archive_template(
    template_id: int,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> None:
    """Archive: no longer offered; loses its links. Drafts made from it keep working."""
    service.archive_template(admin.id, template_id)
