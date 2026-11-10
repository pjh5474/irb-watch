"""Plain data types shared by crawlers, the diff logic and (later) the DB layer."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PostSummary:
    board_key: str
    post_id: str
    title: str
    posted_on: str  # as shown by the site, e.g. "2026.09.03"
    is_secret: bool
    detail_url: str


@dataclass(frozen=True)
class AttachmentRef:
    filename: str
    size_label: str  # as shown by the site, e.g. "66 KB"
    url: str  # re-read from the detail page before every download; do not persist
    # An id the site keeps for this very file across sessions (R-BAY's download key), if any.
    # Lets a file that is never downloaded (too large) still be told apart from a replacement.
    stable_id: str | None = None


@dataclass(frozen=True)
class PostDetail:
    summary: PostSummary
    body_text: str  # normalized: no view count, no prev/next links
    body_hash: str
    attachments: list[AttachmentRef] = field(default_factory=list)


@dataclass(frozen=True)
class DownloadedFile:
    filename: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class BoardSnapshot:
    board_key: str
    declared_total: int
    posts: list[PostDetail]
    secret_posts: list[PostSummary] = field(default_factory=list)
