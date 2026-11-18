"""The daily AI search for IRB dates (slice 2 of the date recommendations; owner, 2026-11-18).

After a scan run, for each hospital whose notice board (board kind 'notice', the hospital's own
board with AI on) was scanned: read the notice posts that are new or changed since the
hospital's cursor and ask the AI for meetings. A hospital with nothing new costs no AI call.

Rules:
  * Only the hospital's own boards feed it (R-BAY, public SNUH): never a study or hand-made
    source (services.sessions.hospital_board). A silent first scan (baseline) is left to the
    admin's ALL-SEARCH.
  * The AI writes NEW proposals only (state proposed, origin ai, no person); it never edits,
    confirms or cancels a session. Users pick one on their step; admins may still review.
  * The cursor (table hospital_session_search) moves only after a search that worked, so a failed
    day is read again; a failure keeps its readable reason there and in the audit log.
  * No job row (no person to own it); one audit entry per hospital is the usage record.
  * Never raises: it runs inside the scan runner's after-run step.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from irb_watch.services.errors import ServiceError
from irb_watch.services.scan import FAILED
from irb_watch.services.sessions import (
    FIND_POSTS,
    NoReadableTextError,
    PostBrief,
    SessionService,
    ranked_posts,
)
from irb_watch.services.users import AuditSink

log = logging.getLogger(__name__)

SEEN_POSTS = 60  # changed posts looked at per hospital (titles first), FIND_POSTS of them sent
FIRST_LOOKBACK = timedelta(days=2)  # a hospital never searched: the posts changed in the last days
MAX_HOSPITALS_PER_RUN = 60  # the rest waits for the next run (its cursor does not move)


@dataclass(frozen=True)
class SearchState:
    hospital_id: int
    last_run_at: str | None = None
    last_ok_at: str | None = None
    last_status: str | None = None  # ok | failed
    last_error: str | None = None
    last_posts: int = 0
    last_proposed: int = 0


class DailySearchStore(Protocol):
    def notice_sources(self, source_ids: list[int]) -> dict[int, list[int]]:
        """Of these sources, the active notice boards with AI on that are a hospital's own
        boards, by hospital."""
        ...

    def changed_posts(self, source_ids: list[int], since: str, limit: int) -> list[PostBrief]:
        """Live posts of these sources whose latest version was saved after `since`, newest first."""
        ...

    def state(self, hospital_id: int) -> SearchState | None: ...
    def save_state(self, state: SearchState) -> None: ...


class ScanReport(Protocol):
    scanned: dict[int, str]
    summaries: dict


class DailySessionSearch:
    def __init__(
        self,
        sessions: SessionService,
        store: DailySearchStore,
        audit: AuditSink,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._store = store
        self._audit = audit
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self._sessions.finder_enabled

    def after_run(self, report: ScanReport) -> dict[str, int]:
        """Search the hospitals this run scanned. Returns counts (for logs and tests)."""
        counts = {"hospitals": 0, "searched": 0, "failed": 0, "nothing_new": 0, "proposed": 0}
        if not self.enabled:
            return counts
        try:
            ids = [sid for sid, status in report.scanned.items() if status != FAILED]
            baseline = {sid for sid in ids if getattr(report.summaries.get(sid), "baseline", False)}
            by_hospital = self._store.notice_sources(ids) if ids else {}
        except Exception:
            log.exception("the daily date search could not list the hospitals to read")
            return counts
        for hospital_id, source_ids in sorted(by_hospital.items())[:MAX_HOSPITALS_PER_RUN]:
            counts["hospitals"] += 1
            try:
                # A board's silent first scan is left to the admin's ALL-SEARCH: its posts are not read.
                fresh = [s for s in source_ids if s not in baseline]
                if fresh:
                    self._one(hospital_id, fresh, counts)
                else:
                    self._start_after_baseline(hospital_id)
            except Exception:  # one hospital never stops the others
                log.exception("the daily date search of hospital %s crashed", hospital_id)
                counts["failed"] += 1
        if len(by_hospital) > MAX_HOSPITALS_PER_RUN:
            log.warning("the daily date search left %d hospital(s) for the next run", len(by_hospital) - MAX_HOSPITALS_PER_RUN)
        return counts

    def _start_after_baseline(self, hospital_id: int) -> None:
        """Only a silent first scan today: the daily search starts from now, so its backlog is not
        read. A hospital that has a state already keeps it (its unread posts stay unread-safe)."""
        if self._store.state(hospital_id) is None:
            now = self._clock().isoformat()
            self._store.save_state(SearchState(hospital_id, now, now, "ok", None, 0, 0))

    def _one(self, hospital_id: int, source_ids: list[int], counts: dict[str, int]) -> None:
        started = self._clock()
        previous = self._store.state(hospital_id)
        since = previous.last_ok_at if previous and previous.last_ok_at else (started - FIRST_LOOKBACK).isoformat()
        changed = self._store.changed_posts(source_ids, since, SEEN_POSTS)
        posts = ranked_posts(changed, FIND_POSTS)
        unread = len(changed) - len(posts)  # the cursor moves past them: told, never silent
        if unread > 0:
            log.warning("the daily date search of hospital %s read %d of %d changed posts", hospital_id, len(posts), len(changed))
        if not posts:
            counts["nothing_new"] += 1
            # Nothing to read: the cursor moves, no AI call, no cost.
            self._store.save_state(SearchState(hospital_id, started.isoformat(), started.isoformat(), "ok", None, 0, 0))
            return
        error: str | None = None
        result: dict = {}
        # Counted before the call (usage per action), like the admin's search. Outside the try: an
        # audit failure is not an AI failure, and the AI is not called without its record.
        self._audit.record(
            "scan", "session.daily_find_started", None, title="Daily IRB date search started",
            payload={"hospital_id": hospital_id, "posts": len(posts), "changed": len(changed)},
        )
        try:
            result = self._sessions.propose_from_posts(hospital_id, posts, None, None)
        except NoReadableTextError:
            result = {"documents_read": 0, "proposed": 0, "skipped": 0}  # nothing to read is not a failure
        except ServiceError as exc:
            error = exc.message[:1000]
        except Exception as exc:
            log.exception("the daily date search of hospital %s failed", hospital_id)
            error = f"the AI service or the database failed ({type(exc).__name__}); it will be tried again at the next scan"
        if error is not None:
            counts["failed"] += 1
            self._store.save_state(SearchState(
                hospital_id, started.isoformat(), previous.last_ok_at if previous else None,
                "failed", error, len(posts), 0,
            ))
        else:
            counts["searched"] += 1
            counts["proposed"] += result.get("proposed", 0)
            self._store.save_state(SearchState(
                hospital_id, started.isoformat(), started.isoformat(), "ok", None,
                result.get("documents_read", len(posts)), result.get("proposed", 0),
            ))
        try:
            self._audit.record(
                "scan", "session.daily_find_finished", None, title="Daily IRB date search finished",
                payload={"hospital_id": hospital_id, "ok": error is None, "posts": len(posts), **result},
            )
        except Exception:
            log.exception("the end of the daily date search of hospital %s could not be logged", hospital_id)
