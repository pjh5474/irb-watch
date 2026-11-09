from fastapi import APIRouter, Depends
from pydantic import BaseModel

from irb_watch.api.deps import get_summary_service, require_admin
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.summaries import SummaryService

router = APIRouter(prefix="/admin/documents", tags=["admin-documents"])


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
