"""The assistant's model: OpenAI's Responses API, streamed, stateless (store=False).

Stateless on purpose: nothing is kept on OpenAI's side, so every call carries the whole
conversation. After a step, only plain items are fed back: the assistant text and the function
calls without their ids (no reasoning items), so the API never asks for items it was not given.
The model name comes from OPENAI_MODEL; nothing is hardcoded.
"""

from collections.abc import Iterator

from openai import OpenAI

from irb_watch.agent.types import ModelEvent, StepEnd, TextDelta, ToolCall
from irb_watch.ai.openai_summarizer import OPENAI_DIRECT_URL


class OpenAIAgentModel:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout: float = 90.0,
        max_output_tokens: int = 1500,
    ) -> None:
        # An explicit address always (see OpenAISummarizer): an empty OPENAI_BASE_URL exported by
        # Docker must never become the address.
        self._client = OpenAI(
            api_key=api_key, base_url=base_url or OPENAI_DIRECT_URL, timeout=timeout
        )
        self.model = model
        self._max_output_tokens = max_output_tokens

    def step(
        self, instructions: str, items: list[dict], tools: list[dict]
    ) -> Iterator[ModelEvent]:
        kwargs: dict = {}
        if tools:
            kwargs["tools"] = tools
        stream = self._client.responses.create(
            model=self.model,
            instructions=instructions,
            input=items,
            store=False,  # keep nothing on OpenAI's side
            stream=True,
            max_output_tokens=self._max_output_tokens,
            **kwargs,
        )
        final = None
        for event in stream:
            kind = getattr(event, "type", "")
            if kind == "response.output_text.delta":
                yield TextDelta(event.delta)
            elif kind == "response.completed":
                final = event.response
            elif kind in ("response.failed", "response.incomplete", "error"):
                raise RuntimeError(f"the model stopped with {kind}")
        if final is None:
            raise RuntimeError("the model stream ended before it finished")

        out: list[dict] = []
        calls: list[ToolCall] = []
        for item in final.output:
            item_type = getattr(item, "type", "")
            if item_type == "function_call":
                arguments = item.arguments or "{}"
                calls.append(ToolCall(item.call_id, item.name, arguments))
                out.append(
                    {
                        "type": "function_call",
                        "call_id": item.call_id,
                        "name": item.name,
                        "arguments": arguments,
                    }
                )
            elif item_type == "message":
                text = "".join(
                    getattr(part, "text", "") or ""
                    for part in item.content
                    if getattr(part, "type", "") == "output_text"
                )
                if text:
                    out.append({"role": "assistant", "content": text})
        yield StepEnd(tuple(out), tuple(calls))
