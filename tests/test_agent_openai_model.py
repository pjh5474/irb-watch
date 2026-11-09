"""The OpenAI wrapper, tested against a fake client that speaks the stream's event shapes.

NOT verified against the real API here: the event names and item fields below follow the
Responses API as installed in this project's SDK, and need one live check (see the report).
"""

from types import SimpleNamespace as NS

import pytest

from irb_watch.agent.openai_model import OpenAIAgentModel
from irb_watch.agent.types import StepEnd, TextDelta, ToolCall


class FakeClient:
    def __init__(self, events) -> None:
        self.events = events
        self.kwargs = None
        self.responses = NS(create=self._create)

    def _create(self, **kwargs):
        self.kwargs = kwargs
        return iter(self.events)


def model_with(events):
    m = OpenAIAgentModel("key", "model-x", None)
    m._client = FakeClient(events)
    return m


def delta(text):
    return NS(type="response.output_text.delta", delta=text)


def completed(*output):
    return NS(type="response.completed", response=NS(output=list(output)))


def message(text):
    return NS(type="message", content=[NS(type="output_text", text=text)])


def call(call_id="c1", name="get_document", arguments='{"document_id": 1}'):
    return NS(type="function_call", call_id=call_id, name=name, arguments=arguments, id="fc_123")


def test_text_is_streamed_and_the_step_ends_with_the_assistant_message():
    m = model_with([delta("Hel"), delta("lo"), completed(message("Hello"))])
    out = list(m.step("rules", [{"role": "user", "content": "hi"}], []))
    assert out[:2] == [TextDelta("Hel"), TextDelta("lo")]
    assert out[2] == StepEnd(({"role": "assistant", "content": "Hello"},), ())


def test_function_calls_are_returned_without_their_server_ids():
    m = model_with([completed(call())])
    end = list(m.step("rules", [], [{"type": "function"}]))[-1]
    assert end.calls == (ToolCall("c1", "get_document", '{"document_id": 1}'),)
    assert end.items == (
        {"type": "function_call", "call_id": "c1", "name": "get_document",
         "arguments": '{"document_id": 1}'},
    )
    assert "id" not in end.items[0]


def test_reasoning_items_are_not_fed_back():
    m = model_with([completed(NS(type="reasoning", id="rs_1"), call())])
    end = list(m.step("rules", [], []))[-1]
    assert [i.get("type") for i in end.items] == ["function_call"]


def test_the_request_keeps_nothing_on_openais_side_and_streams():
    m = model_with([completed(message("x"))])
    list(m.step("rules", [{"role": "user", "content": "hi"}], [{"type": "function", "name": "t"}]))
    k = m._client.kwargs
    assert k["store"] is False and k["stream"] is True
    assert k["model"] == "model-x" and k["instructions"] == "rules"
    assert k["tools"] == [{"type": "function", "name": "t"}]
    assert k["max_output_tokens"] == 1500


def test_with_no_tools_the_tools_argument_is_left_out():
    m = model_with([completed(message("x"))])
    list(m.step("rules", [], []))
    assert "tools" not in m._client.kwargs


@pytest.mark.parametrize("kind", ["response.failed", "response.incomplete", "error"])
def test_a_failed_stream_raises(kind):
    m = model_with([delta("par"), NS(type=kind)])
    with pytest.raises(RuntimeError, match=kind):
        list(m.step("rules", [], []))


def test_a_stream_that_ends_without_completing_raises():
    m = model_with([delta("par")])
    with pytest.raises(RuntimeError, match="before it finished"):
        list(m.step("rules", [], []))


def test_an_empty_base_url_never_becomes_the_address():
    m = OpenAIAgentModel("key", "model-x", "")
    assert str(m._client.base_url).startswith("https://api.openai.com")
