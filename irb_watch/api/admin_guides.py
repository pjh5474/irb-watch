"""Admin: the guide library of the docs generator and which guides each source uses."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from irb_watch.api.deps import get_docgen_service, require_admin
from irb_watch.api.docgen import FileIn, GuideOut
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.docgen import DocgenService, UploadedFile

router = APIRouter(prefix="/admin", tags=["admin-guides"])


class GuideIn(BaseModel):
    title: str = Field(max_length=500)
    file: FileIn


class GuideTextOut(GuideOut):
    body_text: str


@router.get("/guides", response_model=list[GuideOut])
def list_guides(
    include_archived: bool = False,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> list[GuideOut]:
    return [GuideOut.of(g) for g in service.list_guides(admin.id, include_archived)]


@router.post("/guides", status_code=201, response_model=GuideTextOut)
def create_guide(
    body: GuideIn,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> GuideTextOut:
    """Upload a guide file (PDF, HWP, HWPX, DOCX, MD, TXT; at most 5 MB). Only its text is kept;
    the answer shows the text so the admin can check it was read correctly."""
    guide = service.create_guide(
        admin.id, body.title, UploadedFile(body.file.filename, body.file.content_base64)
    )
    return GuideTextOut(**GuideOut.of(guide).model_dump(), body_text=guide.body_text)


@router.delete("/guides/{guide_id}", status_code=204)
def archive_guide(
    guide_id: int,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> None:
    """Archive: the guide is no longer offered and loses its links. Old drafts still name it."""
    service.archive_guide(admin.id, guide_id)


@router.get("/sources/{source_id}/guides", response_model=list[GuideOut])
def source_guides(
    source_id: int,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> list[GuideOut]:
    return [GuideOut.of(g) for g in service.source_guides(admin.id, source_id)]


@router.put("/sources/{source_id}/guides/{guide_id}", status_code=204)
def link_guide(
    source_id: int,
    guide_id: int,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> None:
    service.link_guide(admin.id, source_id, guide_id)


@router.delete("/sources/{source_id}/guides/{guide_id}", status_code=204)
def unlink_guide(
    source_id: int,
    guide_id: int,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> None:
    service.unlink_guide(admin.id, source_id, guide_id)
