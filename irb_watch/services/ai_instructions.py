"""Admin-edited instructions for the AI features: history, restore, reset, and a test run.

Rules (owner decisions 2026-11-10, see docs/decisions.md "Admin-edited AI instructions"):
  * One text per feature: agent (chat), docgen (docs generator), summary (all summaries).
    It is appended after the feature's fixed rules (ai/admin_text.py); it cannot remove them.
  * Every save is a new row; restore copies an old row into a new one; reset saves "".
    Each change is in the audit log (fail closed: no log entry, no change).
  * Changes apply on the next AI call: this process forgets its cached copy at once; other
    processes pick it up within CACHE_SECONDS.
  * Test runs use the unsaved text, show the answer and save nothing. At most TESTS_PER_DAY
    per admin per rolling 24 hours, logged before the call. Admins may test on any source,
    including ones with AI switched off (owner's choice); the log names the document.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from irb_watch.ai.admin_text import FEATURES, MAX_CHARS
from irb_watch.ai.digest import change_digest, document_digest, new_post_digest
from irb_watch.ai.types import CHANGE, DOCUMENT, NEW
from irb_watch.core.diff import describe_change
from irb_watch.docgen.types import DocVersion
from irb_watch.services.errors import (
    ForbiddenError,
    NotFoundError,
    TooManyRequestsError,
    UnavailableError,
    UpstreamError,
    ValidationError,
)
from irb_watch.services.users import AuditSink, UserStore, active_profile

log = logging.getLogger(__name__)

CACHE_SECONDS = 30.0
TESTS_PER_DAY = 30
HISTORY_LIMIT = 50


@dataclass(frozen=True)
class InstructionVersion:
    id: int
    feature: str
    body: str
    created_by: str | None
    created_at: str
    restored_from: int | None = None


@dataclass(frozen=True)
class PreviewResult:
    text: str
    note: str = ""  # e.g. which tools the chat used, or that the input was shortened


class InstructionsStore(Protocol):
    def latest(self, feature: str) -> InstructionVersion | None: ...
    def history(self, feature: str, limit: int) -> list[InstructionVersion]: ...
    def get(self, version_id: int) -> InstructionVersion | None: ...
    def add(
        self,
        feature: str,
        body: str,
        created_by: str,
        restored_from: int | None,
        now: datetime,
    ) -> InstructionVersion: ...
    def count_tests_since(self, admin_id: str, since: datetime) -> int: ...


class InstructionsCache:
    """The text in use per feature, read from the store at most every CACHE_SECONDS.

    A failed read keeps the last known text (or none) for another CACHE_SECONDS and is logged:
    an AI call must not fail, or wait on a down database every time, because the admin text
    could not be read; the fixed rules always apply.

    forget() (after a save in this process) bumps a counter, so a slow read that started before
    the save cannot put the old text back."""

    def __init__(
        self,
        store: InstructionsStore,
        seconds: float = CACHE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._seconds = seconds
        self._monotonic = monotonic
        self._values: dict[str, tuple[float, str]] = {}
        self._generation: dict[str, int] = {}
        self._lock = threading.Lock()

    def get(self, feature: str) -> str:
        now = self._monotonic()
        with self._lock:
            cached = self._values.get(feature)
            if cached is not None and now - cached[0] < self._seconds:
                return cached[1]
            generation = self._generation.get(feature, 0)
        try:
            row = self._store.latest(feature)
            text = row.body if row else ""
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read admin instructions for %s: %s", feature, type(exc).__name__)
            text = cached[1] if cached else ""
        with self._lock:
            if self._generation.get(feature, 0) == generation:
                self._values[feature] = (now, text)
        return text

    def provider(self, feature: str) -> Callable[[], str]:
        return lambda: self.get(feature)

    def forget(self, feature: str) -> None:
        with self._lock:
            self._values.pop(feature, None)
            self._generation[feature] = self._generation.get(feature, 0) + 1


@dataclass(frozen=True)
class Preview:
    """A test run of one feature.

    check(admin id, document id or None, question or summary kind) raises for anything the run
    would be refused for (missing document, wrong kind, empty question); it runs BEFORE the
    test is counted, so a typo never uses up the daily allowance.
    run(admin id, unsaved text, document id or None, question or summary kind) -> result.
    """

    check: Callable[[str, int | None, str], object]
    run: Callable[[str, str, int | None, str], PreviewResult]


class _SummarizerWithExtra(Protocol):
    def summarize(self, kind: str, text: str, *, truncated: bool, extra: str = "") -> str: ...


def summary_preview(
    summarizer: _SummarizerWithExtra, read: Callable[[int], DocVersion | None]
) -> Preview:
    """A test run for summaries. `question` names the kind: change (the newest version against
    the one before), new (as if just published) or document (whole document, the default)."""

    def check(admin_id: str, document_id: int | None, kind: str) -> tuple[str, DocVersion]:
        if document_id is None:
            raise ValidationError("pick a document to test with")
        kind = kind or DOCUMENT
        if kind not in (CHANGE, NEW, DOCUMENT):
            raise ValidationError("the summary kind must be change, new or document")
        doc = read(document_id)
        if doc is None:
            raise NotFoundError("document not found")
        if kind == CHANGE and doc.previous is None:
            raise ValidationError("this document has only one version; test 'new' or 'document'")
        return kind, doc

    def run(admin_id: str, extra: str, document_id: int | None, kind: str) -> PreviewResult:
        kind, doc = check(admin_id, document_id, kind)
        if kind == CHANGE:
            assert doc.previous is not None
            digest = change_digest(describe_change(doc.previous, doc.content), doc.content)
        elif kind == NEW:
            digest = new_post_digest(doc.content)
        else:
            digest = document_digest(doc.content)
        try:
            text = summarizer.summarize(kind, digest.text, truncated=digest.truncated, extra=extra)
        except Exception as exc:  # shown to the admin with a reason
            raise UpstreamError(f"the test run failed ({type(exc).__name__})") from exc
        note = f"{kind} summary of document {document_id}, version {doc.version_no}"
        return PreviewResult(text, note + (" (input shortened)" if digest.truncated else ""))

    return Preview(check, run)


class InstructionsService:
    def __init__(
        self,
        users: UserStore,
        store: InstructionsStore,
        cache: InstructionsCache,
        audit: AuditSink,
        previews: dict[str, Preview] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._users = users
        self._store = store
        self._cache = cache
        self._audit = audit
        self._previews = previews or {}
        self._clock = clock

    def _admin(self, user_id: str) -> None:
        profile = active_profile(self._users, user_id, "this account cannot change AI settings")
        if profile.role != "admin":
            raise ForbiddenError("admin access required")

    @staticmethod
    def _feature(feature: str) -> str:
        if feature not in FEATURES:
            raise NotFoundError(f"unknown AI feature {feature!r}; use one of {', '.join(FEATURES)}")
        return feature

    @staticmethod
    def _clean(body: str) -> str:
        text = (body or "").replace(chr(0), "").strip()
        if len(text) > MAX_CHARS:
            raise ValidationError(f"the instructions are too long (at most {MAX_CHARS} characters)")
        return text

    def current(self, admin_id: str) -> dict[str, InstructionVersion | None]:
        self._admin(admin_id)
        return {f: self._store.latest(f) for f in FEATURES}

    def history(self, admin_id: str, feature: str) -> list[InstructionVersion]:
        self._admin(admin_id)
        return self._store.history(self._feature(feature), HISTORY_LIMIT)

    def _save(
        self, admin_id: str, feature: str, body: str, restored_from: int | None, event: str
    ) -> InstructionVersion:
        old = self._store.latest(feature)
        old_body = old.body if old else ""
        if old is not None and old_body == body:
            return old  # nothing changed; no new row, no log entry
        self._audit.record_required(
            "admin",
            event,
            admin_id,
            payload={
                "feature": feature,
                "old_version_id": old.id if old else None,
                "old_length": len(old_body),
                "new_length": len(body),
                "restored_from": restored_from,
            },
        )
        row = self._store.add(feature, body, admin_id, restored_from, self._clock())
        self._cache.forget(feature)
        return row

    def save(self, admin_id: str, feature: str, body: str) -> InstructionVersion:
        self._admin(admin_id)
        return self._save(admin_id, self._feature(feature), self._clean(body), None, "ai_instructions.changed")

    def reset(self, admin_id: str, feature: str) -> InstructionVersion:
        self._admin(admin_id)
        return self._save(admin_id, self._feature(feature), "", None, "ai_instructions.reset")

    def restore(self, admin_id: str, feature: str, version_id: int) -> InstructionVersion:
        self._admin(admin_id)
        feature = self._feature(feature)
        old = self._store.get(version_id)
        if old is None or old.feature != feature:
            raise NotFoundError("that version does not exist for this feature")
        return self._save(admin_id, feature, old.body, old.id, "ai_instructions.restored")

    def test(
        self,
        admin_id: str,
        feature: str,
        body: str,
        document_id: int | None,
        question: str,
    ) -> PreviewResult:
        """Run the feature once with the UNSAVED text. Nothing is saved."""
        self._admin(admin_id)
        feature = self._feature(feature)
        text = self._clean(body)
        preview = self._previews.get(feature)
        if preview is None:
            raise UnavailableError("AI is not set up on this server (OPENAI_API_KEY and OPENAI_MODEL)")
        question = (question or "").strip()
        preview.check(admin_id, document_id, question)  # refusals here are not counted
        now = self._clock()
        used = self._store.count_tests_since(admin_id, now - timedelta(days=1))
        if used >= TESTS_PER_DAY:
            raise TooManyRequestsError(f"you have used your {TESTS_PER_DAY} test runs for the last 24 hours")
        self._audit.record_required(
            "admin",
            "ai_instructions.tested",
            admin_id,
            payload={"feature": feature, "length": len(text)},
            document_id=document_id,
        )
        return preview.run(admin_id, text, document_id, question)
