from fastapi import APIRouter, Depends
from pydantic import BaseModel

from irb_watch.api.deps import current_user, get_summary_service
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.summaries import SummaryService

router = APIRouter(prefix="/documents", tags=["documents"])


class SummaryOut(BaseModel):
    summary: str
    cached: bool  # True: generated earlier, did not count toward the daily limit
    version_no: int


@router.post("/{document_id}/summary", response_model=SummaryOut)
def summarize_document(
    document_id: int,
    user: AuthUser = Depends(current_user),
    service: SummaryService = Depends(get_summary_service),
) -> SummaryOut:
    """Summary of the whole current document. For users who may read its source, and admins."""
    result = service.summarize_document(user.id, document_id)
    return SummaryOut(summary=result.summary, cached=result.cached, version_no=result.version_no)
