"""Short-lived download links for the original files of stored versions.

The files sit in a private bucket; nobody but the server can reach them. A user asks for a
link to one file (any version), the server applies the same read rule as everywhere else
(admin, or any user for public sources, or an assigned member of a members-only source) and hands back
a signed link that works for 60 seconds. A file the user may not read answers "not found",
so its existence is not revealed. Every link request is logged and capped per user per day.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from irb_watch.services.errors import (
    NotFoundError,
    TooManyRequestsError,
    UpstreamError,
)
from irb_watch.services.users import AuditSink, UserStore, active_profile


@dataclass(frozen=True)
class StoredFile:
    file_id: int
    source_id: int
    document_id: int
    version_no: int
    filename: str
    sha256: str
    storage_path: str | None  # None: the original was never stored


@dataclass(frozen=True)
class DownloadLink:
    url: str
    expires_at: datetime
    filename: str


@dataclass(frozen=True)
class ImageLink:
    file_id: int
    filename: str
    url: str


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
IMAGES_PER_VERSION = 40  # 20 text images plus attached images, more than any post seen
IMAGE_TTL_SECONDS = 600  # the page shows them at once; a long page may still be read later


def is_shown_image(filename: str, hash_only_reason: str | None) -> bool:
    """Images the document page shows: those of the post's text, and attached image files."""
    return hash_only_reason == "inline_image" or filename.lower().endswith(IMAGE_EXTENSIONS)


class DownloadRepo(Protocol):
    def get_file(self, file_id: int) -> StoredFile | None: ...
    def may_read(self, user_id: str, source_id: int, now: datetime) -> bool: ...
    def count_links_since(self, user_id: str, since: datetime) -> int: ...
    def version_images(self, version_id: int) -> tuple[int, int, int, list[StoredFile]] | None:
        """(source id, document id, version no, the version's stored image files) or None."""
        ...
    def count_image_views_since(self, user_id: str, since: datetime) -> int: ...


class UrlSigner(Protocol):
    def sign(self, storage_path: str, filename: str, expires_in: int) -> str: ...
    def sign_inline(self, storage_path: str, expires_in: int) -> str:
        """A link the browser shows in the page (no download disposition)."""
        ...


class DownloadService:
    def __init__(
        self,
        users: UserStore,
        repo: DownloadRepo,
        signer: UrlSigner,
        audit: AuditSink,
        ttl_seconds: int = 60,
        daily_limit: int = 200,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        image_views_per_day: int = 1000,
    ) -> None:
        self._image_views = image_views_per_day
        self._users = users
        self._repo = repo
        self._signer = signer
        self._audit = audit
        self._ttl = ttl_seconds
        self._daily_limit = daily_limit
        self._clock = clock

    def download_link(self, user_id: str, file_id: int) -> DownloadLink:
        profile = active_profile(self._users, user_id, "this account cannot download files")
        is_admin = profile.role == "admin"

        now = self._clock()
        file = self._repo.get_file(file_id)
        if file is None or not (is_admin or self._repo.may_read(user_id, file.source_id, now)):
            raise NotFoundError("file not found")
        if file.storage_path is None:
            raise NotFoundError(
                "the original of this file was not stored, so it cannot be downloaded"
            )

        if not is_admin:
            used = self._repo.count_links_since(user_id, now - timedelta(days=1))
            if used >= self._daily_limit:
                raise TooManyRequestsError(
                    f"daily limit of {self._daily_limit} download links reached; try tomorrow"
                )

        try:
            url = self._signer.sign(file.storage_path, file.filename, self._ttl)
        except Exception as exc:
            raise UpstreamError(f"could not create the download link ({type(exc).__name__})") from exc

        # The log entry is also the daily-cap counter, so it must exist before the link is
        # handed out. If it cannot be written the link is discarded (it was never delivered
        # and dies on its own within seconds).
        self._audit.record_required(
            "user",
            "file.download_link",
            user_id,
            title=file.filename,
            payload={"file_id": file.file_id, "sha256": file.sha256, "version_no": file.version_no},
            source_id=file.source_id,
            document_id=file.document_id,
        )
        return DownloadLink(url, now + timedelta(seconds=self._ttl), file.filename)

    def image_links(self, user_id: str, version_id: int) -> tuple[list[ImageLink], datetime]:
        """Links that show one version's images in the page (owner, 2026-11-16: shown
        automatically). One call per page view, whatever the number of images: the same read
        rule as downloads (not found otherwise), its own daily cap (server-wide default) and
        one log entry per call, written before any link is handed out. Downloads keep their
        own cap and log. Server-wide for now: per-organization limits come with SaaS."""
        profile = active_profile(self._users, user_id, "this account cannot view files")
        is_admin = profile.role == "admin"
        now = self._clock()
        found = self._repo.version_images(version_id)
        if found is None or not (is_admin or self._repo.may_read(user_id, found[0], now)):
            raise NotFoundError("version not found")
        source_id, document_id, version_no, files = found
        files = [f for f in files if f.storage_path][:IMAGES_PER_VERSION]
        expires = now + timedelta(seconds=IMAGE_TTL_SECONDS)
        if not files:
            return [], expires
        if not is_admin and self._repo.count_image_views_since(user_id, now - timedelta(days=1)) >= self._image_views:
            raise TooManyRequestsError(
                f"daily limit of {self._image_views} image views reached; download single files instead"
            )
        try:
            links = [ImageLink(f.file_id, f.filename, self._signer.sign_inline(f.storage_path, IMAGE_TTL_SECONDS))
                     for f in files]  # type: ignore[arg-type]
        except Exception as exc:
            raise UpstreamError(f"could not create the image links ({type(exc).__name__})") from exc
        self._audit.record_required(
            "user",
            "file.image_links",
            user_id,
            title=None,
            payload={"version_no": version_no, "files": [f.file_id for f in files]},
            source_id=source_id,
            document_id=document_id,
        )
        return links, expires
