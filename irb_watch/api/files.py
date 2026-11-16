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
    document: any signed-in user for public sources, assigned members for members-only
    sources (studies), and admins.
    Each request is logged and counted toward a daily cap.
    """
    link = service.download_link(user.id, file_id)
    return DownloadLinkOut(url=link.url, expires_at=link.expires_at.isoformat(), filename=link.filename)


class ImageLinkOut(BaseModel):
    file_id: int
    filename: str
    url: str  # shown in the page; works for about 10 minutes


class ImageLinksOut(BaseModel):
    images: list[ImageLinkOut]
    expires_at: str


@router.post("/versions/{version_id}/image-links", response_model=ImageLinksOut)
def create_image_links(
    version_id: int,
    user: AuthUser = Depends(current_user),
    service: DownloadService = Depends(get_download_service),
) -> ImageLinksOut:
    """Links that show the images of one stored version in the page: images of the post's text
    and attached image files. `version_id` is `document_versions.id`. Same access as reading
    the document (404 otherwise). One call per page view, logged and capped per day on its
    own; downloads keep their own cap."""
    links, expires = service.image_links(user.id, version_id)
    return ImageLinksOut(
        images=[ImageLinkOut(file_id=x.file_id, filename=x.filename, url=x.url) for x in links],
        expires_at=expires.isoformat(),
    )
