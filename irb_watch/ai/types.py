from dataclasses import dataclass, field
from typing import Protocol

NEW = "new"
CHANGE = "change"
DOCUMENT = "document"


@dataclass(frozen=True)
class Classification:
    priority: str  # low | medium | high
    priority_confidence: float
    category: str | None
    category_confidence: float | None
    substantive: float | None  # probability that the change alters a requirement; None if not asked
    raw: dict = field(default_factory=dict)  # answers and model name, for audit; no document text


class Classifier(Protocol):
    def classify(self, text: str, *, include_substantive: bool) -> Classification: ...


class Summarizer(Protocol):
    model: str

    def summarize(self, kind: str, text: str, *, truncated: bool) -> str:
        """kind: 'change' (what changed), 'new' (what a new post is) or 'document'."""
        ...
