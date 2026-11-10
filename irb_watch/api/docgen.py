"""The docs generator's API: a user's own drafts (login token of any active user).

Files are sent as base64 inside JSON (no form-parsing package is needed). The DOCX comes back
as a file download from POST /docgen/drafts/{id}/export.
"""

from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from irb_watch.api.deps import current_user, get_docgen_service
from irb_watch.auth.jwt import AuthUser
from irb_watch.docgen.types import Draft, DraftVersion, Guide
from irb_watch.services.docgen import (
    MAX_EXTRA_GUIDES,
    DocChoice,
    DocgenService,
    DraftDetail,
    UploadedFile,
)

router = APIRouter(prefix="/docgen", tags=["docgen"])

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class StatusOut(BaseModel):
    enabled: bool
    used: int
    limit: int
    reason: str | None = None


class GuideOut(BaseModel):
    id: int
    title: str
    filename: str
    chars: int
    source_ids: list[int]
    created_at: str
    archived_at: str | None = None

    @classmethod
    def of(cls, g: Guide) -> "GuideOut":
        return cls(
            id=g.id,
            title=g.title,
            filename=g.filename,
            chars=len(g.body_text),
            source_ids=list(g.source_ids),
            created_at=g.created_at,
            archived_at=g.archived_at,
        )


class DocRefOut(BaseModel):
    document_id: int
    version_no: int
    source_id: int


class DraftOut(BaseModel):
    id: str
    title: str
    instruction: str
    documents: list[DocRefOut]
    guide_ids: list[int]
    extra_guides: list[str]  # file names
    ai_override_source_ids: list[int]
    created_at: str
    updated_at: str

    @classmethod
    def of(cls, d: Draft) -> "DraftOut":
        return cls(
            id=d.id,
            title=d.title,
            instruction=d.instruction,
            documents=[
                DocRefOut(document_id=r.document_id, version_no=r.version_no, source_id=r.source_id)
                for r in d.document_refs
            ],
            guide_ids=list(d.guide_ids),
            extra_guides=[e.filename for e in d.extra_guides],
            ai_override_source_ids=list(d.ai_override_source_ids),
            created_at=d.created_at,
            updated_at=d.updated_at,
        )


class VersionOut(BaseModel):
    version_no: int
    markdown: str
    origin: str
    instruction: str
    created_at: str

    @classmethod
    def of(cls, v: DraftVersion) -> "VersionOut":
        return cls(
            version_no=v.version_no,
            markdown=v.markdown,
            origin=v.origin,
            instruction=v.instruction,
            created_at=v.created_at,
        )


class DraftDetailOut(BaseModel):
    draft: DraftOut
    versions: list[VersionOut]
    guides: list[GuideOut]

    @classmethod
    def of(cls, d: DraftDetail) -> "DraftDetailOut":
        return cls(
            draft=DraftOut.of(d.draft),
            versions=[VersionOut.of(v) for v in d.versions],
            guides=[GuideOut.of(g) for g in d.guides],
        )


class DocChoiceIn(BaseModel):
    document_id: int
    version_no: int | None = None  # None = the newest version


class FileIn(BaseModel):
    filename: str = Field(max_length=300)
    # 5 MB of file is about 6.7 million base64 characters; the real limit is in the service.
    content_base64: str = Field(max_length=7_000_000)


class DraftIn(BaseModel):
    documents: list[DocChoiceIn] = Field(max_length=50)
    # None: every guide linked to the documents' sources. A list: exactly these (leave some out).
    guide_ids: list[int] | None = Field(default=None, max_length=50)
    extra_guides: list[FileIn] = Field(default_factory=list, max_length=MAX_EXTRA_GUIDES)
    instruction: str = Field(default="", max_length=20_000)
    # Sources with AI switched off whose text the user agreed to send to OpenAI for this draft.
    ai_override_source_ids: list[int] = Field(default_factory=list, max_length=50)


class MarkdownIn(BaseModel):
    markdown: str = Field(max_length=500_000)


class ReviseIn(BaseModel):
    instruction: str = Field(max_length=20_000)
    # Needed only when AI was switched off for a source after the draft was made (409 names it).
    ai_override_source_ids: list[int] = Field(default_factory=list, max_length=50)


@router.get("/status", response_model=StatusOut)
def status(
    user: AuthUser = Depends(current_user), service: DocgenService = Depends(get_docgen_service)
) -> StatusOut:
    return StatusOut(**service.status(user.id))


@router.get("/guides", response_model=list[GuideOut])
def default_guides(
    source_id: list[int] = Query(default_factory=list),
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
) -> list[GuideOut]:
    """The guides a draft from these sources uses by default (sources the user may read)."""
    return [GuideOut.of(g) for g in service.default_guides(user.id, source_id)]


@router.post("/drafts", status_code=201, response_model=DraftDetailOut)
def create_draft(
    body: DraftIn,
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
) -> DraftDetailOut:
    """Writes the first draft with the AI (takes up to a few minutes). 409 names any AI-off
    source that was not ticked."""
    return DraftDetailOut.of(
        service.create_draft(
            user.id,
            [DocChoice(d.document_id, d.version_no) for d in body.documents],
            body.guide_ids,
            [UploadedFile(f.filename, f.content_base64) for f in body.extra_guides],
            body.instruction,
            body.ai_override_source_ids,
        )
    )


@router.get("/drafts", response_model=list[DraftOut])
def list_drafts(
    user: AuthUser = Depends(current_user), service: DocgenService = Depends(get_docgen_service)
) -> list[DraftOut]:
    return [DraftOut.of(d) for d in service.list_drafts(user.id)]


@router.get("/drafts/{draft_id}", response_model=DraftDetailOut)
def get_draft(
    draft_id: UUID,
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
) -> DraftDetailOut:
    return DraftDetailOut.of(service.get_draft(user.id, str(draft_id)))


@router.put("/drafts/{draft_id}/markdown", response_model=VersionOut)
def save_edit(
    draft_id: UUID,
    body: MarkdownIn,
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
) -> VersionOut:
    """Saves a hand edit as a new version (no AI, not counted)."""
    return VersionOut.of(service.save_edit(user.id, str(draft_id), body.markdown))


@router.post("/drafts/{draft_id}/revise", response_model=VersionOut)
def revise(
    draft_id: UUID,
    body: ReviseIn,
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
) -> VersionOut:
    """Asks the AI to change the newest version as instructed; saves the result as a new one."""
    return VersionOut.of(
        service.revise(user.id, str(draft_id), body.instruction, body.ai_override_source_ids)
    )


@router.post("/drafts/{draft_id}/export")
def export_docx(
    draft_id: UUID,
    version_no: int | None = None,
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
) -> Response:
    """A plain DOCX of one version (default: the newest)."""
    content, filename = service.export_docx(user.id, str(draft_id), version_no)
    ascii_name = filename.encode("ascii", "ignore").decode() or "draft.docx"
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    return Response(content, media_type=DOCX_TYPE, headers={"Content-Disposition": disposition})


@router.delete("/drafts/{draft_id}", status_code=204)
def delete_draft(
    draft_id: UUID,
    user: AuthUser = Depends(current_user),
    service: DocgenService = Depends(get_docgen_service),
) -> None:
    service.delete_draft(user.id, str(draft_id))
