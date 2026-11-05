"""The AI step of a scan: classify each new or changed post with Jev, then summarize it with
OpenAI unless a summary is not worth the money. Runs after change detection and before the
results are saved, so the news card and its summary are written together.

Rules (docs/decisions.md):
  * the card is always written; AI only decides what its text says;
  * title-only changes and changes Jev is confident are cosmetic get a template description,
    not an AI summary;
  * when Jev is unsure the post is summarized and its priority is medium;
  * a title-only change is always low priority, whatever Jev says;
  * a failing service never fails the scan: Jev down = no priority/category, OpenAI down =
    template text (updates) or no summary (new posts), and the version records why;
  * OpenAI calls per scan are capped; past the cap the template text is used.
"""

import logging
from dataclasses import dataclass, replace

from irb_watch.ai.digest import Digest, change_digest, new_post_digest
from irb_watch.ai.templates import template_summary
from irb_watch.ai.types import CHANGE, NEW, Classification, Classifier, Summarizer
from irb_watch.core.detect import ChangeEvent, DocumentStore

log = logging.getLogger(__name__)

AI = "ai"
TEMPLATE = "template"
NONE = "none"


@dataclass(frozen=True)
class Enrichment:
    summary: str | None
    priority: str | None
    category: str | None
    source: str  # ai | template | none
    note: str | None = None  # why the AI was not used or failed, never document text


class Enricher:
    def __init__(
        self,
        classifier: Classifier | None,
        summarizer: Summarizer | None,
        *,
        skip_below: float = 0.15,
        min_confidence: float = 0.6,
        max_summaries_per_scan: int = 100,
    ) -> None:
        self._classifier = classifier
        self._summarizer = summarizer
        self._skip_below = skip_below
        self._min_confidence = min_confidence
        self._max_summaries = max_summaries_per_scan

    @property
    def active(self) -> bool:
        return self._classifier is not None or self._summarizer is not None

    def enrich(self, events: list[ChangeEvent], store: DocumentStore) -> dict[str, Enrichment]:
        """Fill summary, priority and category on the new version of each new or updated post.

        Mutates `store` (replaces the latest StoredVersion). Returns what was decided, by post id.
        """
        results: dict[str, Enrichment] = {}
        summaries_left = self._max_summaries
        for event in events:
            if event.kind not in ("new", "updated"):
                continue
            doc = store[event.external_id]
            version = doc.latest
            is_new = event.kind == "new"
            digest = (
                new_post_digest(version.content)
                if is_new
                else change_digest(event.detail, version.content)
            )

            classification, note = self._classify(digest, include_substantive=not is_new)
            priority = self._priority(event, classification)
            template_only = not is_new and self._is_cosmetic(event, classification)

            summary, source, ai_note, summaries_left = self._summary(
                event, version.content.title, digest, template_only, summaries_left
            )
            notes = [n for n in (note, ai_note) if n]
            category = classification.category if classification else None
            enrichment = Enrichment(summary, priority, category, source, "; ".join(notes) or None)
            results[event.external_id] = enrichment

            raw = dict(classification.raw) if classification else {}
            if enrichment.note:
                raw["note"] = enrichment.note
            if digest.truncated:
                raw["input_truncated"] = True
            if self._summarizer is not None and source == AI:
                raw["summary_model"] = self._summarizer.model
            doc.versions[-1] = replace(
                version,
                summary=summary,
                priority=priority,
                category=category,
                summary_source=source,
                classification=raw or None,
            )
        return results

    # -- steps ------------------------------------------------------------------------

    def _classify(
        self, digest: Digest, *, include_substantive: bool
    ) -> tuple[Classification | None, str | None]:
        if self._classifier is None:
            return None, None
        try:
            return self._classifier.classify(digest.text, include_substantive=include_substantive), None
        except Exception as exc:
            log.exception("Jev classification failed")  # recorded with a traceback
            return None, f"classification unavailable ({type(exc).__name__})"

    def _priority(self, event: ChangeEvent, c: Classification | None) -> str | None:
        if event.detail is not None and event.detail.suggested_priority:
            return event.detail.suggested_priority  # title-only: always low
        if c is None:
            return None
        return c.priority if c.priority_confidence >= self._min_confidence else "medium"

    def _is_cosmetic(self, event: ChangeEvent, c: Classification | None) -> bool:
        if event.detail is not None and event.detail.title_only:
            return True
        return bool(
            c is not None
            and c.substantive is not None
            and c.substantive < self._skip_below
            and c.priority == "low"
            and c.priority_confidence >= self._min_confidence
        )

    def _summary(
        self, event: ChangeEvent, title: str, digest: Digest, template_only: bool, left: int
    ) -> tuple[str | None, str, str | None, int]:
        """Returns (summary, source, note, summaries_left)."""

        def fallback(note: str | None) -> tuple[str | None, str, str | None, int]:
            if event.detail is not None:
                return template_summary(event.detail, title), TEMPLATE, note, left
            return None, NONE, note, left

        if template_only:
            return fallback(None)
        if self._summarizer is None:
            return fallback("AI summaries are switched off")
        if left <= 0:
            return fallback("AI summary limit for this scan reached")
        kind = NEW if event.kind == "new" else CHANGE
        try:
            text = self._summarizer.summarize(kind, digest.text, truncated=digest.truncated)
        except Exception as exc:
            log.exception("OpenAI summary failed")  # recorded with a traceback
            return fallback(f"AI summary failed ({type(exc).__name__})")
        return text, AI, None, left - 1
