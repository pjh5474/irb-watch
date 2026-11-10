"""Scan one source: read it, detect changes, save them, log them.

The scan is claimed first (sources.scan_started_at) so two scans of one source cannot
overlap. A failure of the whole scan (layout change, incomplete list, network error)
changes no documents and is recorded on the source as `needs_review` or `failed`.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from irb_watch.ai.enrich import Enricher, Enrichment
from irb_watch.ai.templates import template_summary
from irb_watch.core.detect import (
    ChangeEvent,
    DocumentStore,
    ScanInput,
    detect_changes,
)
from irb_watch.crawlers.errors import (
    CredentialStoreError,
    FetchError,
    IncompleteScanError,
    LayoutChangedError,
    LoginExpiredError,
    LoginRejectedError,
    RobotsDisallowedError,
    ScanTooLargeError,
    SiteUnavailableError,
    TransientFetchError,
)
from irb_watch.crawlers.snuh_reader import FileSink, ScanReading
from irb_watch.services.errors import ConflictError, NotFoundError, ValidationError
from irb_watch.services.sources import Source
from irb_watch.services.users import AuditSink

log = logging.getLogger(__name__)

STALE_SCAN_MINUTES = 30
# Passed to release_scan to leave the source's login pause unchanged.
KEEP_PAUSE = object()
NEEDS_REVIEW = "needs_review"
FAILED = "failed"
OK = "ok"


@dataclass
class LoadedStore:
    """Documents of one source as loaded from storage: only the latest version of each."""

    store: DocumentStore = field(default_factory=dict)
    doc_ids: dict[str, int] = field(default_factory=dict)  # external_id -> database id
    loaded_latest: dict[str, int] = field(default_factory=dict)  # external_id -> version_no
    snapshots: dict[str, tuple] = field(default_factory=dict)  # for detecting changed rows


class SourceReader(Protocol):
    def read(self, file_sink: FileSink) -> ScanReading: ...


class ScanRepo(Protocol):
    def get_source(self, source_id: int) -> Source | None: ...
    def claim_scan(self, source_id: int, now: datetime, stale_minutes: int) -> bool: ...
    def release_scan(
        self,
        source_id: int,
        status: str,
        error: str | None,
        scanned_at: datetime | None,
        paused_reason: "str | None | object" = None,
    ) -> None:
        """paused_reason: a text sets the pause, None clears it, KEEP_PAUSE leaves it as is."""
        ...
    def load_store(self, source_id: int) -> LoadedStore: ...
    def commit_scan(
        self,
        source_id: int,
        loaded: LoadedStore,
        seen: set[str],
        now: datetime,
        cards: "list[ScanCard]",
        status: str,
        error: str | None,
    ) -> dict[str, int]:
        """Save documents, versions, files, cards and the scan result all-or-nothing."""
        ...

    def has_stored_file(self, sha256: str) -> bool: ...


class FileStorage(Protocol):
    def put(self, path: str, content: bytes) -> None: ...


@dataclass(frozen=True)
class ScanCard:
    """One audit-log row (news card) written together with the scan's data."""

    category: str
    event_type: str
    actor_id: str | None
    title: str | None = None
    summary: str | None = None
    payload: dict = field(default_factory=dict)
    priority: str | None = None
    external_id: str | None = None  # the post this card is about; None for scan-level cards


@dataclass
class ScanSummary:
    source_id: int
    status: str  # ok | needs_review | failed
    baseline: bool = False
    new: int = 0
    updated: int = 0
    removed: int = 0
    reappeared: int = 0
    posts_needing_review: int = 0
    error: str | None = None
    # True when the failure was temporary site trouble: the scheduler may retry once later.
    retryable: bool = False
    # Set after a rejected login: scheduled scans skip this source until a person scans it.
    paused_reason: str | None = None


class ScanService:
    def __init__(
        self,
        repo: ScanRepo,
        storage: FileStorage,
        reader_factory,
        audit: AuditSink,
        clock=lambda: datetime.now(UTC),
        enricher: Enricher | None = None,
    ) -> None:
        self._repo = repo
        self._storage = storage
        self._reader_factory = reader_factory
        self._audit = audit
        self._clock = clock
        self._enricher = enricher

    # -- two-phase API: claim in the request, run in the background ------------------

    def begin_scan(self, source_id: int) -> Source:
        """Validate and claim. Raises NotFound / Conflict / Validation immediately."""
        source = self._repo.get_source(source_id)
        if source is None:
            raise NotFoundError("source not found")
        if not source.is_active:
            raise ConflictError("source is inactive")
        if not self._repo.claim_scan(source_id, self._clock(), STALE_SCAN_MINUTES):
            raise ConflictError("a scan of this source is already running")
        return source

    def run_scan(
        self, source: Source, actor_id: str | None, person_started: bool = False
    ) -> ScanSummary:
        """Run a scan that begin_scan() claimed. Always releases the claim.

        person_started: an admin pressed "scan" for this one source. That is how a pause after
        a rejected login is lifted, so the reader may send a remembered login once more."""
        now = self._clock()
        saved = False  # True once the scan's changes were stored
        try:
            summary = self._scan(source, actor_id, now, person_started)
            saved = True
        except LoginRejectedError as exc:
            # The site refused the login. Scheduled scans must not try again (account
            # lockout): they stay paused until a person starts a scan of this source.
            summary = ScanSummary(
                source.id, NEEDS_REVIEW, error=str(exc), paused_reason=str(exc)
            )
        except (
            LayoutChangedError,
            IncompleteScanError,
            RobotsDisallowedError,
            LoginExpiredError,
            ScanTooLargeError,
            CredentialStoreError,  # nothing was sent to the site, so no pause
        ) as exc:
            summary = ScanSummary(source.id, NEEDS_REVIEW, error=str(exc))
        except FetchError as exc:
            # Only temporary site trouble is worth trying again later (see ScanRunner).
            summary = ScanSummary(
                source.id,
                FAILED,
                error=str(exc),
                retryable=isinstance(exc, (SiteUnavailableError, TransientFetchError)),
            )
        except ValidationError as exc:
            summary = ScanSummary(source.id, FAILED, error=exc.message)
        except Exception as exc:
            log.exception("scan of source %s crashed", source.id)
            reason = (str(exc).splitlines() or [""])[0][:200]
            summary = ScanSummary(
                source.id, FAILED, error=f"unexpected error ({type(exc).__name__}): {reason}"
            )

        if summary.status != OK or summary.error:
            self._audit.record(
                "scan",
                "scan.needs_review" if summary.status == NEEDS_REVIEW else "scan.failed",
                actor_id,
                title=f"Scan of {source.name}: {summary.status}",
                payload={"error": summary.error},
                source_id=source.id,
            )
        # last_scan_at marks the baseline: only a scan that stored its results may set it,
        # otherwise a failed first scan would make the next one report every post as new.
        # The pause (after a rejected login) changes only when the scan says something about
        # the login: a saved scan proves it works (clear), a rejection sets it, and every
        # other failure leaves it as it was, because the login was never checked.
        if saved:
            pause = None
        elif summary.paused_reason:
            pause = summary.paused_reason
        else:
            pause = KEEP_PAUSE
        self._repo.release_scan(
            source.id, summary.status, summary.error, now if saved else None, pause
        )
        return summary

    def scan_source(self, source_id: int, actor_id: str | None) -> ScanSummary:
        return self.run_scan(self.begin_scan(source_id), actor_id)

    # -- the scan itself -------------------------------------------------------------

    def _scan(
        self, source: Source, actor_id: str | None, now: datetime, person_started: bool = False
    ) -> ScanSummary:
        stored_this_run: set[str] = set()

        def file_sink(sha256: str, filename: str, content: bytes) -> str:
            # The object is named by its hash: upload only when that hash is new.
            if sha256 not in stored_this_run and not self._repo.has_stored_file(sha256):
                self._storage.put(sha256, content)
            stored_this_run.add(sha256)
            return sha256

        reader = self._reader_factory(source)
        mark = getattr(reader, "person_started", None)  # only readers that remember logins
        if person_started and mark is not None:
            mark()
        try:
            reading = reader.read(file_sink)
        finally:
            close = getattr(reader, "close", None)
            if close is not None:
                close()

        loaded = self._repo.load_store(source.id)
        is_first_scan = source.last_scan_at is None and not loaded.store
        # Remember how each post looked before this scan, so a persistent problem is
        # announced once instead of on every scan.
        before = {ext: (d.status, d.review_reason) for ext, d in loaded.store.items()}
        result = detect_changes(
            loaded.store,
            ScanInput(
                observations=reading.observations,
                now=now,
                is_first_scan=is_first_scan,
                failures=reading.failures,
                unreadable_ids=frozenset(reading.unreadable_ids),
            ),
        )
        # Summaries, priority and category are decided before saving, so a version and its
        # news card are written together (the audit log cannot be edited afterwards).
        # A source with AI switched off (a study: its documents may be confidential) sends
        # nothing to the AI services; its cards get the plain template line instead.
        use_ai = self._enricher is not None and source.ai_enabled
        enrichments = self._enricher.enrich(result.events, loaded.store) if use_ai else {}
        seen = {o.external_id for o in reading.observations}

        # Everything below is saved in ONE transaction by commit_scan: documents, versions,
        # files, these news cards and the scan result. Any failure saves nothing.
        cards = [
            self._event_card(
                event, actor_id, enrichments.get(event.external_id), plain=not source.ai_enabled
            )
            for event in result.events
        ]

        titles = {o.external_id: o.content.title for o in reading.observations}
        for external_id, reason in result.needs_review.items():
            if before.get(external_id) == (NEEDS_REVIEW, reason):
                continue  # already announced with this very reason
            doc = loaded.store.get(external_id)
            title = titles.get(external_id) or (
                doc.latest.content.title if doc else f"Post {external_id}"
            )
            cards.append(
                ScanCard(
                    "document",
                    "document.needs_review",
                    actor_id,
                    title=title,
                    summary=reason,
                    payload={"external_id": external_id, "reason": reason},
                    external_id=external_id,
                )
            )

        counts = {k: sum(1 for e in result.events if e.kind == k) for k in
                  ("new", "updated", "removed", "reappeared")}
        review = len(result.needs_review)
        summary = ScanSummary(
            source.id,
            NEEDS_REVIEW if review else OK,
            baseline=result.baseline,
            posts_needing_review=review,
            error=_failure_text(result.needs_review) if review else None,
            **counts,
        )
        cards.append(
            ScanCard(
                "scan",
                "scan.finished",
                actor_id,
                title=f"Scan of {source.name} finished",
                payload={
                    "baseline": summary.baseline,
                    "posts": len(reading.observations),
                    "new": summary.new,
                    "updated": summary.updated,
                    "removed": summary.removed,
                    "reappeared": summary.reappeared,
                    "posts_needing_review": review,
                },
            )
        )
        self._repo.commit_scan(
            source.id, loaded, seen, now, cards, summary.status, summary.error
        )
        return summary

    def _event_card(
        self,
        event: ChangeEvent,
        actor_id: str | None,
        enrichment: Enrichment | None,
        plain: bool = False,
    ) -> "ScanCard":
        detail = event.detail
        payload: dict = {"version_no": event.version_no}
        if detail is not None:
            payload["title_changed"] = detail.title_change is not None
            payload["text_changed"] = detail.body_diff is not None
            payload["files"] = {f.filename: f.kind for f in detail.meaningful_file_changes}
        priority = detail.suggested_priority if detail else None
        summary = None
        if enrichment is not None:
            priority = enrichment.priority
            summary = enrichment.summary
            payload["category"] = enrichment.category
            payload["summary_source"] = enrichment.source
            if enrichment.note:
                payload["ai_note"] = enrichment.note
        elif plain and detail is not None:
            # AI is off for this source: a factual template line, no text leaves the server.
            summary = template_summary(detail, event.title)
            payload["summary_source"] = "template"
        return ScanCard(
            "document",
            f"document.{event.kind}",
            actor_id,
            title=event.title,
            summary=summary,
            payload=payload,
            priority=priority,
            external_id=event.external_id,
        )


def _failure_text(failures: dict[str, str], limit: int = 5) -> str:
    items = [f"post {post_id}: {reason}" for post_id, reason in list(failures.items())[:limit]]
    more = len(failures) - limit
    return "; ".join(items) + (f" (+{more} more)" if more > 0 else "")
