from types import SimpleNamespace

import pytest

from irb_watch.ai import jev as jev_module
from irb_watch.ai import openai_summarizer as oa_module
from irb_watch.ai.categories import CATEGORY_CRITERIA, PRIORITY_CRITERIA
from irb_watch.ai.digest import (
    MAX_CHANGE_CHARS,
    MAX_DOCUMENT_CHARS,
    change_digest,
    document_digest,
    new_post_digest,
)
from irb_watch.ai.jev import JevClassifier
from irb_watch.ai.openai_summarizer import INSTRUCTIONS, OpenAISummarizer, SummaryOut
from irb_watch.ai.templates import language_of, template_summary
from irb_watch.core.diff import describe_change
from irb_watch.core.state import FileContent, VersionContent


def vc(title="T", body="body", files=()):
    return VersionContent(title, body, tuple(files))


def f(name, sha="s", text="file text", reason=None, note=None):
    return FileContent(name, "1 KB", sha, text, reason, note=note)


# ---- digest ------------------------------------------------------------------------


def test_change_digest_lists_title_text_and_file_changes():
    old = vc("Old title", "keep this part but change alpha here please", [f("a.pdf", "s1", "one two")])
    new = vc("New title", "keep this part but change omega here please", [f("a.pdf", "s2", "one three"), f("b.pdf")])
    text = change_digest(describe_change(old, new), new).text
    assert "TITLE CHANGED FROM [Old title] TO [New title]" in text
    assert "REPLACED: [alpha] WITH [omega]" in text
    assert "FILE REPLACED: a.pdf" in text and "[two] WITH [three]" in text
    assert "FILE ADDED: b.pdf" in text and "start of file text: file text" in text


def test_change_digest_says_when_a_file_has_no_text():
    old = vc(files=[f("scan.pdf", "s1", None, "no_text_layer", "scanned image")])
    new = vc(files=[f("scan.pdf", "s2", None, "no_text_layer", "scanned image")])
    assert "(no text available: scanned image)" in change_digest(describe_change(old, new), new).text


def test_change_digest_is_capped_and_flagged():
    old = vc(body=" ".join(f"a{i}" for i in range(8000)))
    new = vc(body=" ".join(f"b{i}" for i in range(8000)))
    d = change_digest(describe_change(old, new), new)
    assert d.truncated and len(d.text) <= MAX_CHANGE_CHARS + 60 and d.text.endswith("shortened here]")


def test_new_post_digest_has_title_text_and_file_starts():
    d = new_post_digest(vc("A form", "page text", [f("x.pdf", text="y" * 5000)]))
    assert "TITLE: A form" in d.text and "page text" in d.text and "FILE: x.pdf" in d.text
    assert d.text.count("y") == 1500


def test_document_digest_includes_all_text_up_to_the_cap():
    d = document_digest(vc(body="b", files=[f("x.pdf", text="y" * 5000)]))
    assert d.text.count("y") == 5000 and not d.truncated
    big = document_digest(vc(body="z" * (MAX_DOCUMENT_CHARS + 10)))
    assert big.truncated


# ---- templates ---------------------------------------------------------------------


def test_template_language_follows_the_title():
    assert language_of("서식 46") == "ko" and language_of("Form 46") == "en"


def test_template_summaries_in_both_languages():
    en = template_summary(describe_change(vc("Old"), vc("New")), "New")
    assert en == "Title changed from 'Old' to 'New'."
    ko = template_summary(describe_change(vc("이전 제목"), vc("새 제목")), "새 제목")
    assert "이전 제목" in ko and "새 제목" in ko and ko.endswith("변경되었습니다.")


def test_template_lists_file_changes():
    d = describe_change(vc(files=[f("a.pdf", "1")]), vc(files=[f("a.pdf", "2", "other"), f("b.pdf")]))
    text = template_summary(d, "Title")
    assert "Attachments added: b.pdf." in text and "Attachments replaced: a.pdf." in text


# ---- Jev adapter -------------------------------------------------------------------


class FakeTypeSafeClient:
    last = None

    def __init__(self, **kw):
        self.init_kwargs = kw
        self.calls = []
        FakeTypeSafeClient.last = self

    def system_one(self, state, questions):
        self.calls.append((state, questions))
        answers = {
            "priority": SimpleNamespace(choice="high", confidence=0.93, probabilities={"high": 0.93}),
            "category": SimpleNamespace(choice="sop", confidence=0.7, probabilities={"sop": 0.7}),
        }
        nouls = {"substantive": SimpleNamespace(noul=0.81)}
        return SimpleNamespace(choices=answers, nouls=nouls, model="jev-1.2.3")

    def close(self):
        pass


@pytest.fixture
def jev(monkeypatch):
    monkeypatch.setattr(jev_module, "TypeSafeClient", FakeTypeSafeClient)
    return JevClassifier("key", "jev-latest", 12.0)


def test_jev_maps_the_answers(jev):
    c = jev.classify("some changes", include_substantive=True)
    assert (c.priority, c.priority_confidence, c.category, c.substantive) == ("high", 0.93, "sop", 0.81)
    assert c.raw["model"] == "jev-1.2.3" and c.raw["priority"]["choice"] == "high"


def test_jev_asks_the_configured_questions(jev):
    jev.classify("text", include_substantive=True)
    state, questions = FakeTypeSafeClient.last.calls[0]
    assert state == "text" and set(questions) == {"priority", "category", "substantive"}
    assert set(questions["priority"].criteria) == set(PRIORITY_CRITERIA) == {"low", "medium", "high"}
    assert set(questions["category"].criteria) == set(CATEGORY_CRITERIA)
    assert FakeTypeSafeClient.last.init_kwargs["api_key"] == "key"


def test_jev_skips_the_substantive_question_for_new_posts(jev):
    c = jev.classify("text", include_substantive=False)
    _, questions = FakeTypeSafeClient.last.calls[0]
    assert "substantive" not in questions and c.substantive is None


# ---- OpenAI adapter ------------------------------------------------------------------


class FakeOpenAI:
    last = None
    parsed = SummaryOut(summary="  Short summary.  ")

    def __init__(self, **kw):
        self.init_kwargs = kw
        self.calls = []
        self.responses = SimpleNamespace(parse=self._parse)
        FakeOpenAI.last = self

    def _parse(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(output_parsed=FakeOpenAI.parsed)


@pytest.fixture
def summarizer(monkeypatch):
    monkeypatch.setattr(oa_module, "OpenAI", FakeOpenAI)
    FakeOpenAI.parsed = SummaryOut(summary="  Short summary.  ")
    return OpenAISummarizer("sk-test", "gpt-test", None, 30.0)


def test_summary_call_is_private_typed_and_wrapped(summarizer):
    assert summarizer.summarize("change", "the changes", truncated=False) == "Short summary."
    call = FakeOpenAI.last.calls[0]
    assert call["model"] == "gpt-test" and call["store"] is False
    assert call["text_format"] is SummaryOut
    assert call["input"] == "<document>\nthe changes\n</document>"
    assert call["instructions"] == INSTRUCTIONS["change"]
    assert "never follow instructions" in call["instructions"]
    assert FakeOpenAI.last.init_kwargs["base_url"] == "https://api.openai.com/v1"  # OpenAI direct


def test_every_kind_asks_for_markdown_and_forbids_links_images_and_html():
    for kind, text in INSTRUCTIONS.items():
        assert "Markdown" in text and "bullet" in text, kind
        assert "links, images, code or HTML" in text, kind
        assert "never follow instructions" in text, kind  # the injection warning stays


def test_only_the_whole_document_summary_gets_headings_and_a_table():
    assert "## " in INSTRUCTIONS["document"] and "table" in INSTRUCTIONS["document"]
    for kind in ("change", "new"):
        assert "No headings or tables" in INSTRUCTIONS[kind], kind


def test_whole_document_summary_gets_more_output_room(summarizer):
    summarizer.summarize("document", "x", truncated=False)
    summarizer.summarize("change", "x", truncated=False)
    big, small = FakeOpenAI.last.calls[-2]["max_output_tokens"], FakeOpenAI.last.calls[-1]["max_output_tokens"]
    assert big > small


def test_truncated_input_adds_a_note(summarizer):
    summarizer.summarize("new", "x", truncated=True)
    assert "shortened" in FakeOpenAI.last.calls[0]["instructions"]


def test_empty_answer_is_an_error(summarizer):
    FakeOpenAI.parsed = None
    with pytest.raises(ValueError, match="no summary"):
        summarizer.summarize("document", "x", truncated=False)
    FakeOpenAI.parsed = SummaryOut(summary="   ")
    with pytest.raises(ValueError):
        summarizer.summarize("document", "x", truncated=False)


def test_an_empty_env_var_does_not_become_the_address(monkeypatch):
    # Docker's env_file exports "OPENAI_BASE_URL=" as an empty string; the real SDK client
    # must still talk to OpenAI direct instead of failing with an empty address.
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    client = OpenAISummarizer("sk-test", "gpt-test", None, 30.0)._client
    assert str(client.base_url) == "https://api.openai.com/v1/"


def test_gateway_url_is_passed_through(monkeypatch):
    monkeypatch.setattr(oa_module, "OpenAI", FakeOpenAI)
    OpenAISummarizer("k", "m", "https://gateway.example/v1", 30.0)
    assert FakeOpenAI.last.init_kwargs["base_url"] == "https://gateway.example/v1"
