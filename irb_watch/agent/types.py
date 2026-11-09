"""Small shared types for the assistant: what the model returns, what the browser receives."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str  # the model's JSON text; parsed and checked by the tool box


@dataclass(frozen=True)
class StepEnd:
    """The end of one model call: the items to feed back on the next call and the tool calls."""

    items: tuple[dict, ...]
    calls: tuple[ToolCall, ...]


ModelEvent = TextDelta | StepEnd


class AgentModel(Protocol):
    def step(
        self, instructions: str, items: list[dict], tools: list[dict]
    ) -> Iterator[ModelEvent]:
        """One model call, streamed: TextDelta events, then exactly one StepEnd."""
        ...


@dataclass(frozen=True)
class Event:
    """One server-sent event for the browser: text | tool | action | done | error."""

    kind: str
    data: dict
