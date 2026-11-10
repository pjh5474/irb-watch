from fastapi import APIRouter, Depends
from pydantic import BaseModel

from irb_watch.api.deps import get_review_service, get_summary_service, require_admin
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.reviews import ReviewItem, ReviewService
from irb_watch.services.summaries import SummaryService

router = APIRouter(prefix="/admin/documents", tags=["admin-documents"])


class ReviewOut(BaseModel):
    document_id: int
    source_id: int
    source_name: str
    title: str
    review_reason: str
    # True: only file warnings, which an admin may mark OK. False: the post failed to load.
    file_warning: bool

    @classmethod
    def of(cls, item: ReviewItem) -> "ReviewOut":
        return cls(**item.__dict__)


@router.get("/reviews", response_model=list[ReviewOut])
def list_reviews(
    admin: AuthUser = Depends(require_admin),
    service: ReviewService = Depends(get_review_service),
) -> list[ReviewOut]:
    """Documents in needs_review across all sources (newest first, at most 500)."""
    return [ReviewOut.of(item) for item in service.list()]


@router.post("/{document_id}/review-ok", response_model=ReviewOut)
def acknowledge_review(
    document_id: int,
    admin: AuthUser = Depends(require_admin),
    service: ReviewService = Depends(get_review_service),
) -> ReviewOut:
    """Mark the document's file warnings as OK. The document leaves needs_review; later scans
    that find the very same warnings stay quiet, a different warning flags it again. 409 for a
    post that failed to load (not a file warning) or when a scan changed it meanwhile. Logged
    first, fail closed (`document.review_acknowledged`)."""
    return ReviewOut.of(service.acknowledge(admin.id, document_id))


class RegenerateOut(BaseModel):
    summary: str
    version_no: int
    replaced: bool  # True when an earlier saved summary was overwritten


@router.post("/{document_id}/summary", response_model=RegenerateOut)
def regenerate_summary(
    document_id: int,
    admin: AuthUser = Depends(require_admin),
    service: SummaryService = Depends(get_summary_service),
) -> RegenerateOut:
    """Write the whole-document summary of the current version again and replace the saved one.

    The saved summary is shared by every user. Admins are exempt from the daily limit. If the
    AI call fails the old summary stays. Logged as `summary.regenerated`.
    """
    result = service.regenerate_document(admin.id, document_id)
    return RegenerateOut(
        summary=result.summary, version_no=result.version_no, replaced=result.replaced
    )
