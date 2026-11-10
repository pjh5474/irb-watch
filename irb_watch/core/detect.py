"""Decide what happened on a source since the last scan.

Rules (docs/decisions.md):
  * first scan of a source = silent baseline, no events;
  * a post missing from a complete scan is only marked removed after 2 consecutive misses;
  * a post that failed to load is marked needs_review and keeps its old state: it is never
    counted as missing, unchanged or deleted;
  * unreadable posts (secret posts) are neither seen nor missing;
  * a title change alone is an update (low priority); file bytes that changed while the
    extracted text did not are recorded as a silent 'resaved' version.

`detect_changes` updates `store` in place; the database layer will persist the same
decisions later.
"""

from dataclasses import dataclass, field
from datetime import datetime

from irb_watch.core.diff import ChangeDetail, describe_change
from irb_watch.core.state import (
    BASELINE,
    CURRENT,
    NEEDS_REVIEW,
    NEW,
    REMOVED,
    RESAVED,
    UPDATED,
    StoredDocument,
    StoredVersion,
    VersionContent,
    warning_key,
)

REMOVAL_AFTER_MISSES = 2

DocumentStore = dict[str, StoredDocument]  # external_id -> document


@dataclass(frozen=True)
class Observation:
    external_id: str
    source_url: str
    posted_on: str | None
    content: VersionContent
    # Problems with the post that do not stop it being tracked, e.g. an attachment that is
    # password-protected. Any warning puts the document in needs_review with these reasons.
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScanInput:
    observations: list[Observation]
    now: datetime
    is_first_scan: bool = False
    complete: bool = True  # False when the list could not be read in full: skip removal logic
    failures: dict[str, str] = field(default_factory=dict)  # external_id -> reason
    unreadable_ids: frozenset[str] = frozenset()  # e.g. secret posts


@dataclass(frozen=True)
class ChangeEvent:
    kind: str  # 'new' | 'updated' | 'removed' | 'reappeared'
    external_id: str
    title: str
    version_no: int | None = None
    detail: ChangeDetail | None = None  # set for 'updated'


@dataclass
class DetectionResult:
    events: list[ChangeEvent] = field(default_factory=list)
    needs_review: dict[str, str] = field(default_factory=dict)
    baseline: bool = False


def detect_changes(store: DocumentStore, scan: ScanInput) -> DetectionResult:
    ids = [o.external_id for o in scan.observations]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate external_id in scan observations")

    result = DetectionResult(baseline=scan.is_first_scan)
    seen = set(ids)

    for obs in scan.observations:
        doc = store.get(obs.external_id)
        if doc is None:
            _add_new(store, obs, scan, result)
        else:
            _update_existing(doc, obs, scan, result)

    for external_id, reason in scan.failures.items():
        doc = store.get(external_id)
        if doc is not None:
            doc.status = NEEDS_REVIEW
            doc.review_reason = reason
        result.needs_review[external_id] = reason

    if scan.complete:
        skipped = seen | scan.failures.keys() | scan.unreadable_ids
        for doc in store.values():
            if doc.external_id not in skipped and doc.status != REMOVED:
                _count_miss(doc, scan, result)

    return result


def _add_new(store: DocumentStore, obs: Observation, scan: ScanInput, result: DetectionResult):
    doc = StoredDocument(obs.external_id, obs.source_url, obs.posted_on, scan.now)
    _apply_warnings(doc, obs, result)
    change_type = BASELINE if scan.is_first_scan else NEW
    doc.versions.append(StoredVersion(1, scan.now, change_type, obs.content))
    store[obs.external_id] = doc
    if not scan.is_first_scan:
        result.events.append(ChangeEvent("new", obs.external_id, obs.content.title, 1))


def _update_existing(
    doc: StoredDocument, obs: Observation, scan: ScanInput, result: DetectionResult
):
    was_removed = doc.status == REMOVED
    doc.miss_count = 0
    doc.removed_at = None
    doc.source_url = obs.source_url
    doc.posted_on = obs.posted_on
    _apply_warnings(doc, obs, result)

    previous = doc.latest
    if previous.content.fingerprint() != obs.content.fingerprint():
        detail = describe_change(previous.content, obs.content)
        version_no = previous.version_no + 1
        if detail.only_resaved:
            doc.versions.append(StoredVersion(version_no, scan.now, RESAVED, obs.content))
        else:
            doc.versions.append(StoredVersion(version_no, scan.now, UPDATED, obs.content))
            result.events.append(
                ChangeEvent("updated", doc.external_id, obs.content.title, version_no, detail)
            )

    if was_removed:
        result.events.append(
            ChangeEvent("reappeared", doc.external_id, obs.content.title, doc.latest.version_no)
        )


def _apply_warnings(doc: StoredDocument, obs: Observation, result: DetectionResult) -> None:
    """A post seen this scan is `current`, or `needs_review` with the reasons if it has warnings.

    File warnings an admin already marked OK (the same text about the same files) keep it
    `current`: the files still show their notes, but nobody is asked again. Other text, or a
    replaced file with the same problem, flags it again."""
    reason = "; ".join(obs.warnings) if obs.warnings else None
    if reason is not None and warning_key(reason, obs.content.files) != doc.acknowledged_key:
        doc.status = NEEDS_REVIEW
        doc.review_reason = reason
        result.needs_review[obs.external_id] = reason
    else:
        doc.status = CURRENT
        doc.review_reason = None


def _count_miss(doc: StoredDocument, scan: ScanInput, result: DetectionResult) -> None:
    doc.miss_count += 1
    if doc.miss_count >= REMOVAL_AFTER_MISSES:
        doc.status = REMOVED
        doc.removed_at = scan.now
        result.events.append(
            ChangeEvent("removed", doc.external_id, doc.latest.content.title, doc.latest.version_no)
        )
