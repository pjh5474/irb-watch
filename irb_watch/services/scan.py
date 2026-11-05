"""Scan one source: read it, detect changes, save them, log them.

The scan is claimed first (sources.scan_started_at) so two scans of one source cannot
overlap. A failure of the whole scan (layout change, incomplete list, network error)
changes no documents and is recorded on the source as `needs_review` or `failed`.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from irb_watch.core.detect import (
    ChangeEvent,
    DocumentStore,
    ScanInput,
    detect_changes,
)
from irb_watch.crawlers.errors import (
    FetchError,
    IncompleteScanError,
    LayoutChangedError,
    RobotsDisallowedError,
)
from irb_watch.crawlers.snuh_reader import FileSink, ScanReading
from irb_watch.services.errors import ConflictError, NotFoundError, ValidationError
from irb_watch.services.sources import Source
from irb_watch.services.users import AuditSink

log = logging.getLogger(__name__)

STALE_SCAN_MINUTES = 30
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
        self, source_id: int, status: str, error: str | None, scanned_at: datetime | None
    ) -> None: ...
    def load_store(self, source_id: int) -> LoadedStore: ...
    def persist(
        self, source_id: int, loaded: LoadedStore, seen: set[str], now: datetime
    ) -> dict[str, int]: ...
    def has_stored_file(self, sha256: str) -> bool: ...


class FileStorage(Protocol):
    def put(self, path: str, content: bytes) -> None: ...


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


class ScanService:
    def __init__(
        self,
        repo: ScanRepo,
        storage: FileStorage,
        reader_factory,
        audit: AuditSink,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self._repo = repo
        self._storage = storage
        self._reader_factory = reader_factory
        self._audit = audit
        self._clock = clock

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

    def run_scan(self, source: Source, actor_id: str | None) -> ScanSummary:
        """Run a scan that begin_scan() claimed. Always releases the claim."""
        now = self._clock()
        saved = False  # True once the scan's changes were stored
        try:
            summary = self._scan(source, actor_id, now)
            saved = True
        except (LayoutChangedError, IncompleteScanError, RobotsDisallowedError) as exc:
            summary = ScanSummary(source.id, NEEDS_REVIEW, error=str(exc))
        except FetchError as exc:
            summary = ScanSummary(source.id, FAILED, error=str(exc))
        except ValidationError as exc:
            summary = ScanSummary(source.id, FAILED, error=exc.message)
        except Exception as exc:
            log.exception("scan of source %s crashed", source.id)
            summary = ScanSummary(source.id, FAILED, error=f"unexpected error ({type(exc).__name__})")

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
        self._repo.release_scan(source.id, summary.status, summary.error, now if saved else None)
        return summary

    def scan_source(self, source_id: int, actor_id: str | None) -> ScanSummary:
        return self.run_scan(self.begin_scan(source_id), actor_id)

    # -- the scan itself -------------------------------------------------------------

    def _scan(self, source: Source, actor_id: str | None, now: datetime) -> ScanSummary:
        stored_this_run: set[str] = set()

        def file_sink(sha256: str, filename: str, content: bytes) -> str:
            # The object is named by its hash: upload only when that hash is new.
            if sha256 not in stored_this_run and not self._repo.has_stored_file(sha256):
                self._storage.put(sha256, content)
            stored_this_run.add(sha256)
            return sha256

        reader = self._reader_factory(source)
        try:
            reading = reader.read(file_sink)
        finally:
            close = getattr(reader, "close", None)
            if close is not None:
                close()

        loaded = self._repo.load_store(source.id)
        is_first_scan = source.last_scan_at is None and not loaded.store
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
        seen = {o.external_id for o in reading.observations}
        doc_ids = self._repo.persist(source.id, loaded, seen, now)

        for event in result.events:
            self._log_event(source, event, doc_ids, actor_id)

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
        self._audit.record(
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
            source_id=source.id,
        )
        return summary

    def _log_event(
        self, source: Source, event: ChangeEvent, doc_ids: dict[str, int], actor_id: str | None
    ) -> None:
        detail = event.detail
        payload: dict = {"version_no": event.version_no}
        if detail is not None:
            payload["title_changed"] = detail.title_change is not None
            payload["text_changed"] = detail.body_diff is not None
            payload["files"] = {f.filename: f.kind for f in detail.meaningful_file_changes}
        self._audit.record(
            "document",
            f"document.{event.kind}",
            actor_id,
            title=event.title,
            payload=payload,
            source_id=source.id,
            document_id=doc_ids.get(event.external_id),
            priority=detail.suggested_priority if detail else None,
        )


def _failure_text(failures: dict[str, str], limit: int = 5) -> str:
    items = [f"post {post_id}: {reason}" for post_id, reason in list(failures.items())[:limit]]
    more = len(failures) - limit
    return "; ".join(items) + (f" (+{more} more)" if more > 0 else "")
