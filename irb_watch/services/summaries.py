"""Summary of a whole document, requested by a signed-in user.

Rules: the user must be active and may read the document's source (members-only sources
only for users an admin assigned to them) or be an admin;
a cached summary is returned for free; a new one counts toward a per-user daily limit.
Documents the user may not see answer "not found", so their existence is not revealed.
Admins can also write the summary again (regenerate_document), exempt from the limit.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from irb_watch.ai.digest import document_digest
from irb_watch.ai.types import DOCUMENT, Summarizer
from irb_watch.core.state import VersionContent
from irb_watch.services.errors import (
    ForbiddenError,
    NotFoundError,
    TooManyRequestsError,
    UnavailableError,
    UpstreamError,
)
from irb_watch.services.users import AuditSink, UserStore, active_profile

AI_OFF_FOR_SOURCE = "AI summaries are switched off for this source"


@dataclass(frozen=True)
class DocumentForSummary:
    document_id: int
    source_id: int
    version_id: int
    version_no: int
    content: VersionContent
    document_summary: str | None
    summary_at: str | None = None  # when document_summary was saved (ISO text), if known
    # False: an admin switched AI off for this source (study documents may be confidential).
    ai_enabled: bool = True


@dataclass(frozen=True)
class SummaryResult:
    summary: str
    cached: bool
    version_no: int


@dataclass(frozen=True)
class RegenerateResult:
    summary: str
    version_no: int
    replaced: bool  # True when an earlier saved summary was overwritten


class SummaryRepo(Protocol):
    def get_latest(self, document_id: int) -> DocumentForSummary | None: ...
    def may_read(self, user_id: str, source_id: int, now: datetime) -> bool: ...
    def count_requests_since(self, user_id: str, since: datetime) -> int: ...
    def save_summary(self, version_id: int, text: str, now: datetime) -> None: ...
    def record_request(self, user_id: str, version_id: int, now: datetime) -> None: ...


class SummaryService:
    def __init__(
        self,
        users: UserStore,
        repo: SummaryRepo,
        summarizer: Summarizer | None,
        audit: AuditSink,
        daily_limit: int = 20,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._users = users
        self._repo = repo
        self._summarizer = summarizer
        self._audit = audit
        self._daily_limit = daily_limit
        self._clock = clock

    def summarize_document(self, user_id: str, document_id: int) -> SummaryResult:
        profile = active_profile(self._users, user_id, "this account cannot request summaries")

        now = self._clock()
        doc = self._repo.get_latest(document_id)
        if doc is None:
            raise NotFoundError("document not found")
        # Same rule as the database policies: a members-only source is readable only by
        # users an admin assigned to it.
        if profile.role != "admin" and not self._repo.may_read(user_id, doc.source_id, now):
            raise NotFoundError("document not found")

        if doc.document_summary:
            return SummaryResult(doc.document_summary, True, doc.version_no)

        if not doc.ai_enabled:
            raise UnavailableError(AI_OFF_FOR_SOURCE)
        if self._summarizer is None:
            raise UnavailableError("AI summaries are not switched on for this server")

        used = self._repo.count_requests_since(user_id, now - timedelta(days=1))
        if used >= self._daily_limit:
            raise TooManyRequestsError(
                f"daily limit of {self._daily_limit} new summaries reached; cached ones are free"
            )

        digest = document_digest(doc.content)
        try:
            text = self._summarizer.summarize(DOCUMENT, digest.text, truncated=digest.truncated)
        except Exception as exc:
            # Not counted against the limit and not saved: the user can simply try again.
            raise UpstreamError(f"the summary service failed ({type(exc).__name__})") from exc

        self._repo.save_summary(doc.version_id, text, now)
        self._repo.record_request(user_id, doc.version_id, now)
        self._audit.record(
            "user",
            "summary.requested",
            user_id,
            title=doc.content.title,
            payload={"version_no": doc.version_no, "truncated": digest.truncated},
            source_id=doc.source_id,
            document_id=document_id,
        )
        return SummaryResult(text, False, doc.version_no)

    def regenerate_document(self, user_id: str, document_id: int) -> RegenerateResult:
        """Admin only: write the whole-document summary again and replace the saved one.

        The saved summary is shared by every user, so the new text shows for all of them.
        Only the current version can be redone. Admins are exempt from the daily limit, so
        nothing is counted. A failed generation leaves the old summary untouched. The audit
        entry records who did it, when, and the old and new lengths (not the old text).
        """
        profile = active_profile(self._users, user_id, "this account cannot request summaries")
        if profile.role != "admin":
            raise ForbiddenError("admin access required")
        if self._summarizer is None:
            raise UnavailableError("AI summaries are not switched on for this server")

        now = self._clock()
        doc = self._repo.get_latest(document_id)
        if doc is None:
            raise NotFoundError("document not found")
        if not doc.ai_enabled:
            raise UnavailableError(AI_OFF_FOR_SOURCE)

        digest = document_digest(doc.content)
        try:
            text = self._summarizer.summarize(DOCUMENT, digest.text, truncated=digest.truncated)
        except Exception as exc:
            raise UpstreamError(f"the summary service failed ({type(exc).__name__})") from exc

        self._repo.save_summary(doc.version_id, text, now)
        self._audit.record(
            "admin",
            "summary.regenerated",
            user_id,
            title=doc.content.title,
            payload={
                "version_no": doc.version_no,
                "truncated": digest.truncated,
                "had_summary": bool(doc.document_summary),
                "old_length": len(doc.document_summary or ""),
                "old_summary_at": doc.summary_at,
                "new_length": len(text),
            },
            source_id=doc.source_id,
            document_id=document_id,
        )
        return RegenerateResult(text, doc.version_no, replaced=bool(doc.document_summary))
