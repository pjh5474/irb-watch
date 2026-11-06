from fastapi import APIRouter, Depends
from pydantic import BaseModel

from irb_watch.api.deps import current_user, get_download_service
from irb_watch.auth.jwt import AuthUser
from irb_watch.services.downloads import DownloadService

router = APIRouter(prefix="/files", tags=["files"])


class DownloadLinkOut(BaseModel):
    url: str  # open it right away; it works for a short time only
    expires_at: str
    filename: str


@router.post("/{file_id}/download-url", response_model=DownloadLinkOut)
def create_download_link(
    file_id: int,
    user: AuthUser = Depends(current_user),
    service: DownloadService = Depends(get_download_service),
) -> DownloadLinkOut:
    """Short-lived link to the original of one stored file (any version).

    `file_id` is `document_version_files.id`. Needs the same access as reading the
    document: followers (plus a valid proof of access for members-only sources) and admins.
    Each request is logged and counted toward a daily cap.
    """
    link = service.download_link(user.id, file_id)
    return DownloadLinkOut(url=link.url, expires_at=link.expires_at.isoformat(), filename=link.filename)
