"""Admin: the guide library of the docs generator and which guides each source uses."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from irb_watch.api.deps import get_docgen_service, require_admin
from irb_watch.api.docgen import FileIn, GuideOut
from irb_watch.auth.jwt import AuthUser
from irb_watch.docgen.types import Guide, GuideRevision
from irb_watch.services.docgen import MAX_TITLE_CHARS, DocgenService, UploadedFile

router = APIRouter(prefix="/admin", tags=["admin-guides"])


class GuideIn(BaseModel):
    title: str = Field(max_length=MAX_TITLE_CHARS)  # the same limit as the service and the table
    file: FileIn


class GuideTextOut(GuideOut):
    body_text: str

    @classmethod
    def of_text(cls, g: Guide) -> "GuideTextOut":
        return cls(**GuideOut.of(g).model_dump(), body_text=g.body_text)


class GuidePatch(BaseModel):
    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)
    is_global: bool | None = None


class GuideFileIn(BaseModel):
    file: FileIn


class GuideHospitalsIn(BaseModel):
    hospital_ids: list[int] = Field(max_length=1000)


class RevisionOut(BaseModel):
    id: int
    filename: str
    body_text: str
    chars: int
    valid_from: str
    replaced_by: str | None
    replaced_at: str

    @classmethod
    def of(cls, r: GuideRevision) -> "RevisionOut":
        return cls(
            id=r.id,
            filename=r.filename,
            body_text=r.body_text,
            chars=len(r.body_text),
            valid_from=r.valid_from,
            replaced_by=r.replaced_by,
            replaced_at=r.replaced_at,
        )


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
    return GuideTextOut.of_text(guide)


@router.get("/guides/{guide_id}", response_model=GuideTextOut)
def get_guide(
    guide_id: int,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> GuideTextOut:
    """One guide with its current text."""
    return GuideTextOut.of_text(service.guide(admin.id, guide_id))


@router.patch("/guides/{guide_id}", response_model=GuideOut)
def update_guide(
    guide_id: int,
    body: GuidePatch,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> GuideOut:
    """Rename, and/or make global (a default of every draft for every site) or site-specific."""
    return GuideOut.of(service.update_guide(admin.id, guide_id, body.title, body.is_global))


@router.put("/guides/{guide_id}/file", response_model=GuideTextOut)
def replace_guide_file(
    guide_id: int,
    body: GuideFileIn,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> GuideTextOut:
    """Replace the guide's file; the old text stays in its history. Answers the new text."""
    guide = service.replace_guide_file(
        admin.id, guide_id, UploadedFile(body.file.filename, body.file.content_base64)
    )
    return GuideTextOut.of_text(guide)


@router.get("/guides/{guide_id}/history", response_model=list[RevisionOut])
def guide_history(
    guide_id: int,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> list[RevisionOut]:
    """Earlier texts of the guide, newest first."""
    return [RevisionOut.of(r) for r in service.guide_history(admin.id, guide_id)]


@router.put("/guides/{guide_id}/hospitals", response_model=GuideOut)
def set_guide_hospitals(
    guide_id: int,
    body: GuideHospitalsIn,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> GuideOut:
    """The full list of hospitals that use this guide by default (replaces the current list)."""
    return GuideOut.of(service.set_guide_hospitals(admin.id, guide_id, body.hospital_ids))


@router.delete("/guides/{guide_id}", status_code=204)
def archive_guide(
    guide_id: int,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> None:
    """Archive: the guide is no longer offered and loses its links. Old drafts still name it."""
    service.archive_guide(admin.id, guide_id)


@router.get("/hospitals/{hospital_id}/guides", response_model=list[GuideOut])
def hospital_guides(
    hospital_id: int,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> list[GuideOut]:
    return [GuideOut.of(g) for g in service.hospital_guides(admin.id, hospital_id)]


@router.put("/hospitals/{hospital_id}/guides/{guide_id}", status_code=204)
def link_guide(
    hospital_id: int,
    guide_id: int,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> None:
    service.link_guide(admin.id, hospital_id, guide_id)


@router.delete("/hospitals/{hospital_id}/guides/{guide_id}", status_code=204)
def unlink_guide(
    hospital_id: int,
    guide_id: int,
    admin: AuthUser = Depends(require_admin),
    service: DocgenService = Depends(get_docgen_service),
) -> None:
    service.unlink_guide(admin.id, hospital_id, guide_id)
