"""In-memory model of what we know about each document, and how versions are picked.

The database tables (documents, document_versions, and a files table to be added) mirror
these types; the change-detection logic works on them without touching storage.
"""

from dataclasses import dataclass, field
from datetime import datetime

from irb_watch.core.hashing import sha256_hex

CURRENT = "current"
REMOVED = "removed"
NEEDS_REVIEW = "needs_review"

# Version change types: baseline = silent first scan, new = appeared on a later scan,
# updated = meaningful change, resaved = file bytes changed but the text did not.
BASELINE = "baseline"
NEW = "new"
UPDATED = "updated"
RESAVED = "resaved"


@dataclass(frozen=True)
class FileContent:
    filename: str
    size_label: str
    sha256: str  # hash of the file bytes
    text: str | None  # normalized extracted text; None when tracked by file hash only
    hash_only_reason: str | None = None  # e.g. 'no_text_layer', 'unsupported_format'
    storage_path: str | None = None  # object name of the original in the private bucket
    note: str | None = None  # why there is no text, in words users can read

    @property
    def text_hash(self) -> str | None:
        return sha256_hex(self.text.encode("utf-8")) if self.text is not None else None


@dataclass(frozen=True)
class VersionContent:
    title: str
    body_text: str  # normalized page text, without view counts or prev/next links
    files: tuple[FileContent, ...] = ()

    def fingerprint(self) -> tuple:
        """Equal fingerprints mean 'nothing changed'. Order of files does not matter."""
        return (
            self.title,
            sha256_hex(self.body_text.encode("utf-8")),
            tuple(sorted((f.filename, f.sha256) for f in self.files)),
        )


@dataclass(frozen=True)
class StoredVersion:
    version_no: int
    created_at: datetime
    change_type: str
    content: VersionContent


@dataclass
class StoredDocument:
    external_id: str
    source_url: str
    posted_on: str | None
    first_seen_at: datetime
    status: str = CURRENT
    miss_count: int = 0
    removed_at: datetime | None = None
    review_reason: str | None = None  # why the status is needs_review, for users to read
    versions: list[StoredVersion] = field(default_factory=list)

    @property
    def latest(self) -> StoredVersion:
        return self.versions[-1]

    def get_version(self, version_no: int) -> StoredVersion | None:
        return next((v for v in self.versions if v.version_no == version_no), None)

    def version_as_of(self, moment: datetime) -> StoredVersion | None:
        """The version that was current at `moment` (latest created at or before it)."""
        candidates = [v for v in self.versions if v.created_at <= moment]
        return max(candidates, key=lambda v: v.created_at, default=None)
