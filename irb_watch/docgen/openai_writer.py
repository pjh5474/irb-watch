"""Write a draft with one plain OpenAI Responses API call and a typed answer. No tools."""

from openai import OpenAI
from pydantic import BaseModel

from irb_watch.ai.openai_summarizer import OPENAI_DIRECT_URL
from irb_watch.docgen.types import Written


class DraftOut(BaseModel):
    title: str
    markdown: str


class OpenAIDraftWriter:
    def __init__(
        self, api_key: str, model: str, base_url: str | None = None, timeout: float = 180.0
    ) -> None:
        # Always an explicit address; see OpenAISummarizer for why.
        self._client = OpenAI(
            api_key=api_key, base_url=base_url or OPENAI_DIRECT_URL, timeout=timeout
        )
        self.model = model

    def write(self, instructions: str, input_text: str) -> Written:
        response = self._client.responses.parse(
            model=self.model,
            instructions=instructions,
            input=input_text,
            text_format=DraftOut,
            store=False,  # do not keep the request or answer on OpenAI's side
            # A whole form in Korean needs room; a cut-off answer would fail to parse.
            max_output_tokens=12_000,
        )
        parsed = response.output_parsed
        if parsed is None or not parsed.markdown.strip():
            raise ValueError("the model returned no draft")
        return Written(parsed.title.strip()[:80], parsed.markdown.strip())
