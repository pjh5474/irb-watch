"""Stand-ins for Jev and OpenAI."""

from irb_watch.ai.types import Classification


def classification(
    priority="high", conf=0.9, category="sop", substantive=0.9, category_conf=0.8
) -> Classification:
    return Classification(
        priority=priority,
        priority_confidence=conf,
        category=category,
        category_confidence=category_conf,
        substantive=substantive,
        raw={"model": "jev-test", "priority": {"choice": priority, "confidence": conf}},
    )


class FakeClassifier:
    def __init__(self, result: Classification | Exception | None = None) -> None:
        self.result = result if result is not None else classification()
        self.calls: list[tuple[str, bool]] = []

    def classify(self, text, *, include_substantive):
        self.calls.append((text, include_substantive))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeSummarizer:
    model = "gpt-test"

    def __init__(self, result: str | Exception = "AI summary.") -> None:
        self.result = result
        self.calls: list[tuple[str, str, bool]] = []

    def summarize(self, kind, text, *, truncated):
        self.calls.append((kind, text, truncated))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result
