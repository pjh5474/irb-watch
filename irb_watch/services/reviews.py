"""Documents that need review, and an admin marking their file warnings as OK.

Decided with the owner (2026-11-10): only FILE warnings can be marked OK (an attachment too
big to download, unreadable, damaged or password-protected, or a broken zip). A post that
failed to load keeps its old state and is fixed in code, so it cannot be cleared here. "OK"
lasts until the warning changes: the warning text and the hashes of the flagged files are
stored (core/state.warning_key); later scans with the same key stay quiet, other text or a
replaced file flags the document again (core/detect.py).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from irb_watch.core.state import FLAGGED_REASONS, FileContent, warning_key
from irb_watch.services.errors import ConflictError, NotFoundError
from irb_watch.services.users import AuditSink

LIST_LIMIT = 500


@dataclass(frozen=True)
class ReviewRow:
    document_id: int
    source_id: int
    source_name: str
    title: str
    status: str
    review_reason: str | None
    files: tuple[FileContent, ...]  # the files of the latest version, in saved order


@dataclass(frozen=True)
class ReviewItem:
    document_id: int
    source_id: int
    source_name: str
    title: str
    review_reason: str
    # True when the reason is only file warnings (an admin may mark it OK).
    file_warning: bool


class ReviewStore(Protocol):
    def needing_review(self, limit: int) -> list[ReviewRow]: ...
    def get(self, document_id: int) -> ReviewRow | None: ...
    def acknowledge(
        self, document_id: int, reason: str, key: str, admin_id: str, now: datetime
    ) -> bool:
        """Clear the review only if the document still has exactly this reason, and store the
        approval. False (nothing changed) when a scan replaced the reason meanwhile."""
        ...

    def undo(self, document_id: int, reason: str) -> None:
        """Put the review back and drop the approval (when it could not be logged)."""
        ...


def is_file_warning(reason: str | None, files: tuple[FileContent, ...]) -> bool:
    """True when the reason is exactly one "<file>: <note>" warning per flagged file, in the
    order the files were saved (the order a scan writes them). Notes never contain ";"
    (crawlers/attachments.plain_note), so the parts split back exactly. Matched by file name,
    so a reworded note (for example after the size limit changed) is still a file warning;
    anything else in the text (a post that failed to load, an extra part) makes it False."""
    flagged = [f.filename for f in files if f.hash_only_reason in FLAGGED_REASONS]
    if not reason or not flagged:
        return False
    parts = reason.split("; ")
    if len(parts) != len(flagged):
        return False
    return all(
        part.startswith(f"{name}: ") and part[len(name) + 2 :].strip() and ";" not in part
        for part, name in zip(parts, flagged, strict=True)
    )


class ReviewService:
    def __init__(
        self,
        store: ReviewStore,
        audit: AuditSink,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._audit = audit
        self._clock = clock

    def list(self) -> list[ReviewItem]:
        return [
            ReviewItem(
                row.document_id,
                row.source_id,
                row.source_name,
                row.title,
                row.review_reason or "",
                is_file_warning(row.review_reason, row.files),
            )
            for row in self._store.needing_review(LIST_LIMIT)
        ]

    def acknowledge(self, admin_id: str, document_id: int) -> ReviewItem:
        row = self._store.get(document_id)
        if row is None:
            raise NotFoundError("document not found")
        if row.status != "needs_review" or not row.review_reason:
            raise ConflictError("this document does not need review (any more)")
        if not is_file_warning(row.review_reason, row.files):
            raise ConflictError(
                "only file warnings can be marked OK; this document failed to load and keeps "
                "its old state until the cause is fixed"
            )
        reason = row.review_reason
        key = warning_key(reason, row.files)
        # First the change (conditional: refused if a scan replaced the warning meanwhile, so
        # nothing is logged for an OK that did not happen), then the audit entry. If the entry
        # cannot be written the change is undone: an OK without a record is never left behind.
        if not self._store.acknowledge(document_id, reason, key, admin_id, self._clock()):
            raise ConflictError("the warning changed meanwhile (a scan ran); look again")
        try:
            self._audit.record_required(
                "admin",
                "document.review_acknowledged",
                admin_id,
                title=f"File warnings marked OK: {row.title}",
                payload={"reason": reason[:2000]},
                source_id=row.source_id,
                document_id=row.document_id,
            )
        except Exception:
            self._store.undo(document_id, reason)
            raise
        return ReviewItem(row.document_id, row.source_id, row.source_name, row.title, reason, True)
