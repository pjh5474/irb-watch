"""Build the AI parts from settings. A missing key or model switches that part off; scans and
the rest of the API keep working without it."""

import logging
from functools import lru_cache

from irb_watch.ai.enrich import Enricher
from irb_watch.ai.jev import JevClassifier
from irb_watch.ai.openai_summarizer import OpenAISummarizer
from irb_watch.ai.types import Classifier, Summarizer
from irb_watch.config import Settings, get_settings

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


def build_summarizer(settings: Settings | None = None) -> Summarizer | None:
    s = settings or get_settings()
    return _summarizer(s.openai_api_key, s.openai_model, s.openai_base_url, s.openai_timeout_seconds)


def build_classifier(settings: Settings | None = None) -> Classifier | None:
    s = settings or get_settings()
    return _classifier(s.typesafe_api_key, s.typesafe_model, s.typesafe_timeout_seconds)


def build_enricher(settings: Settings | None = None) -> Enricher:
    s = settings or get_settings()
    return Enricher(
        build_classifier(s),
        build_summarizer(s),
        skip_below=s.ai_skip_below,
        min_confidence=s.ai_min_confidence,
        max_summaries_per_scan=s.max_llm_calls_per_run,
    )
