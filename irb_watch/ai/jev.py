"""Classify a change with Jev (TypeSafe): priority, how substantive it is, and its category."""

from typesafe_sdk import Choice, Noul, TypeSafeClient

from irb_watch.ai.categories import (
    CATEGORY_CRITERIA,
    CATEGORY_QUESTION,
    PRIORITY_CRITERIA,
    PRIORITY_QUESTION,
    SUBSTANTIVE_STATEMENT,
)
from irb_watch.ai.types import Classification


class JevClassifier:
    def __init__(self, api_key: str, model: str = "jev-latest", timeout: float = 30.0) -> None:
        self._client = TypeSafeClient(api_key=api_key, model=model, timeout=timeout)
        self.model = model

    def close(self) -> None:
        self._client.close()

    def classify(self, text: str, *, include_substantive: bool) -> Classification:
        questions = {
            "priority": Choice(instructions=PRIORITY_QUESTION, criteria=PRIORITY_CRITERIA),
            "category": Choice(instructions=CATEGORY_QUESTION, criteria=CATEGORY_CRITERIA),
        }
        if include_substantive:
            questions["substantive"] = Noul(instructions=SUBSTANTIVE_STATEMENT)

        response = self._client.system_one(state=text, questions=questions)

        priority = response.choices["priority"]
        category = response.choices["category"]
        substantive = response.nouls["substantive"].noul if include_substantive else None
        return Classification(
            priority=priority.choice,
            priority_confidence=float(priority.confidence),
            category=category.choice,
            category_confidence=float(category.confidence),
            substantive=None if substantive is None else float(substantive),
            raw={
                "model": getattr(response, "model", None) or self.model,
                "priority": {
                    "choice": priority.choice,
                    "confidence": priority.confidence,
                    "probabilities": priority.probabilities,
                },
                "category": {
                    "choice": category.choice,
                    "confidence": category.confidence,
                    "probabilities": category.probabilities,
                },
                "substantive": substantive,
            },
        )
