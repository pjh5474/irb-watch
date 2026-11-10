"""Admin: the instructions added to each AI feature (chat assistant, docs generator, summaries).

The fixed safety rules stay in code; this text is appended after them. Changes apply on the next
AI call, with no restart.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from irb_watch.api.deps import get_instructions_service, require_admin
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.ai_instructions import InstructionsService, InstructionVersion

router = APIRouter(prefix="/admin/ai", tags=["admin-ai"])


class VersionOut(BaseModel):
    id: int
    feature: str
    body: str
    created_by: str | None
    created_at: str
    restored_from: int | None

    @classmethod
    def of(cls, v: InstructionVersion) -> "VersionOut":
        return cls(
            id=v.id,
            feature=v.feature,
            body=v.body,
            created_by=v.created_by,
            created_at=v.created_at,
            restored_from=v.restored_from,
        )


class CurrentOut(BaseModel):
    feature: str
    current: VersionOut | None  # None: never edited (no added instructions)


class BodyIn(BaseModel):
    body: str = Field(max_length=20_000)  # the real limit (4,000) is checked by the service


class TestIn(BaseModel):
    body: str = Field(max_length=20_000)
    document_id: int | None = None
    # chat: the question; docs: an optional request; summary: the kind (change, new, document)
    question: str = Field(default="", max_length=20_000)


class TestOut(BaseModel):
    text: str
    note: str


@router.get("/instructions", response_model=list[CurrentOut])
def current(
    admin: AuthUser = Depends(require_admin),
    service: InstructionsService = Depends(get_instructions_service),
) -> list[CurrentOut]:
    return [
        CurrentOut(feature=f, current=VersionOut.of(v) if v else None)
        for f, v in service.current(admin.id).items()
    ]


@router.get("/instructions/{feature}/history", response_model=list[VersionOut])
def history(
    feature: str,
    admin: AuthUser = Depends(require_admin),
    service: InstructionsService = Depends(get_instructions_service),
) -> list[VersionOut]:
    """The last 50 versions, newest first."""
    return [VersionOut.of(v) for v in service.history(admin.id, feature)]


@router.put("/instructions/{feature}", response_model=VersionOut)
def save(
    feature: str,
    body: BodyIn,
    admin: AuthUser = Depends(require_admin),
    service: InstructionsService = Depends(get_instructions_service),
) -> VersionOut:
    """Save as the new version (live on the next AI call). Logged first."""
    return VersionOut.of(service.save(admin.id, feature, body.body))


@router.post("/instructions/{feature}/reset", response_model=VersionOut)
def reset(
    feature: str,
    admin: AuthUser = Depends(require_admin),
    service: InstructionsService = Depends(get_instructions_service),
) -> VersionOut:
    """Back to the default: no added instructions (saved as a new, empty version)."""
    return VersionOut.of(service.reset(admin.id, feature))


@router.post("/instructions/{feature}/restore/{version_id}", response_model=VersionOut)
def restore(
    feature: str,
    version_id: int,
    admin: AuthUser = Depends(require_admin),
    service: InstructionsService = Depends(get_instructions_service),
) -> VersionOut:
    """Make an older version current again (copied into a new version)."""
    return VersionOut.of(service.restore(admin.id, feature, version_id))


@router.post("/instructions/{feature}/test", response_model=TestOut)
def test(
    feature: str,
    body: TestIn,
    admin: AuthUser = Depends(require_admin),
    service: InstructionsService = Depends(get_instructions_service),
) -> TestOut:
    """Run the feature once with the UNSAVED text and show the answer; nothing is saved.
    30 test runs per admin per 24 hours."""
    result = service.test(admin.id, feature, body.body, body.document_id, body.question)
    return TestOut(text=result.text, note=result.note)
