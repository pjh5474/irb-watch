"""Build the AI parts from settings. A missing key or model switches that part off; scans and
the rest of the API keep working without it."""

import logging
from collections.abc import Callable
from functools import lru_cache

from irb_watch.agent.openai_model import OpenAIAgentModel
from irb_watch.ai.enrich import Enricher
from irb_watch.ai.jev import JevClassifier
from irb_watch.ai.openai_summarizer import OpenAISummarizer
from irb_watch.ai.types import Classifier, Summarizer
from irb_watch.config import Settings, get_settings
from irb_watch.docgen.openai_writer import OpenAIDraftWriter

log = logging.getLogger(__name__)


@lru_cache
def _summarizer(api_key: str, model: str, base_url: str, timeout: float) -> Summarizer | None:
    if not api_key or not model:
        log.warning("OpenAI summaries are off: set OPENAI_API_KEY and OPENAI_MODEL to enable them")
        return None
    return OpenAISummarizer(api_key, model, base_url or None, timeout)


@lru_cache
def _classifier(api_key: str, model: str, timeout: float) -> Classifier | None:
    if not api_key:
        log.warning("Jev classification is off: set TYPESAFE_API_KEY to enable it")
        return None
    return JevClassifier(api_key, model, timeout)


class AdminInstructedSummarizer:
    """Adds the admins' current summary instructions to every call (read through a provider, so
    a change applies without a restart)."""

    def __init__(self, inner: OpenAISummarizer, extra: Callable[[], str]) -> None:
        self._inner = inner
        self._extra = extra
        self.model = inner.model

    def summarize(self, kind: str, text: str, *, truncated: bool) -> str:
        return self._inner.summarize(kind, text, truncated=truncated, extra=self._extra())


def build_summarizer(
    settings: Settings | None = None, extra: Callable[[], str] | None = None
) -> Summarizer | None:
    """extra: returns the admins' added summary instructions; None = none."""
    s = settings or get_settings()
    base = _summarizer(s.openai_api_key, s.openai_model, s.openai_base_url, s.openai_timeout_seconds)
    if base is None or extra is None or not isinstance(base, OpenAISummarizer):
        return base
    return AdminInstructedSummarizer(base, extra)


@lru_cache
def _agent_model(api_key: str, model: str, base_url: str, timeout: float) -> OpenAIAgentModel | None:
    if not api_key or not model:
        log.warning("The assistant is off: set OPENAI_API_KEY and OPENAI_MODEL to enable it")
        return None
    return OpenAIAgentModel(api_key, model, base_url or None, max(timeout, 90.0))


def build_agent_model(settings: Settings | None = None) -> OpenAIAgentModel | None:
    s = settings or get_settings()
    return _agent_model(s.openai_api_key, s.openai_model, s.openai_base_url, s.openai_timeout_seconds)


@lru_cache
def _draft_writer(
    api_key: str, model: str, base_url: str, timeout: float
) -> OpenAIDraftWriter | None:
    if not api_key or not model:
        log.warning("The docs generator is off: set OPENAI_API_KEY and OPENAI_MODEL to enable it")
        return None
    # A whole form takes longer than a summary.
    return OpenAIDraftWriter(api_key, model, base_url or None, max(timeout, 180.0))


def build_draft_writer(settings: Settings | None = None) -> OpenAIDraftWriter | None:
    s = settings or get_settings()
    return _draft_writer(s.openai_api_key, s.openai_model, s.openai_base_url, s.openai_timeout_seconds)


def build_classifier(settings: Settings | None = None) -> Classifier | None:
    s = settings or get_settings()
    return _classifier(s.typesafe_api_key, s.typesafe_model, s.typesafe_timeout_seconds)


def build_enricher(
    settings: Settings | None = None, extra: Callable[[], str] | None = None
) -> Enricher:
    s = settings or get_settings()
    return Enricher(
        build_classifier(s),
        build_summarizer(s, extra),
        skip_below=s.ai_skip_below,
        min_confidence=s.ai_min_confidence,
        max_summaries_per_scan=s.max_llm_calls_per_run,
    )
