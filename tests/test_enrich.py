from datetime import UTC, datetime, timedelta

import pytest

from irb_watch.ai.enrich import Enricher
from irb_watch.core.detect import Observation, ScanInput, detect_changes
from irb_watch.core.state import FileContent, VersionContent
from tests.fakes_ai import FakeClassifier, FakeSummarizer, classification

T0 = datetime(2026, 11, 5, tzinfo=UTC)


def obs(external_id="1", title="Title", body="old body text", files=()):
    return Observation(
        external_id, f"http://x/{external_id}", "2026-09-03",
        VersionContent(title, body, tuple(files)),
    )


def scan_changes(*, first, second):
    """Baseline `first`, then scan `second`; return (store, events)."""
    store: dict = {}
    detect_changes(store, ScanInput(list(first), T0, is_first_scan=True))
    result = detect_changes(store, ScanInput(list(second), T0 + timedelta(days=1)))
    return store, result.events


def enricher(classifier=None, summarizer=None, **kw):
    return Enricher(classifier, summarizer, **kw)


# ---- updated posts --------------------------------------------------------------------


def test_substantive_change_is_summarized_by_ai_and_classified():
    store, events = scan_changes(first=[obs()], second=[obs(body="new body text")])
    jev, ai = FakeClassifier(classification("high", 0.9, "sop", 0.9)), FakeSummarizer("It changed.")
    out = enricher(jev, ai).enrich(events, store)

    e = out["1"]
    assert (e.summary, e.priority, e.category, e.source) == ("It changed.", "high", "sop", "ai")
    v = store["1"].latest
    assert (v.summary, v.priority, v.category, v.summary_source) == ("It changed.", "high", "sop", "ai")
    assert v.classification["summary_model"] == "gpt-test"
    assert ai.calls[0][0] == "change" and jev.calls[0][1] is True


def test_only_the_changes_are_sent_not_the_whole_text():
    long_text = "word " * 5000
    store, events = scan_changes(
        first=[obs(body=long_text + "alpha")], second=[obs(body=long_text + "omega")]
    )
    ai = FakeSummarizer()
    enricher(FakeClassifier(), ai).enrich(events, store)
    sent = ai.calls[0][1]
    assert "REPLACED: [alpha] WITH [omega]" in sent and len(sent) < 1000


def test_title_only_change_gets_a_template_and_is_always_low():
    store, events = scan_changes(first=[obs(title="Old")], second=[obs(title="New")])
    jev, ai = FakeClassifier(classification("high", 0.95)), FakeSummarizer()
    e = enricher(jev, ai).enrich(events, store)["1"]
    assert e.source == "template" and e.priority == "low"
    assert "Old" in e.summary and "New" in e.summary
    assert ai.calls == []


def test_confident_cosmetic_change_gets_a_template_without_ai():
    store, events = scan_changes(first=[obs()], second=[obs(body="old body text.")])
    jev = FakeClassifier(classification("low", 0.9, substantive=0.05))
    ai = FakeSummarizer()
    e = enricher(jev, ai).enrich(events, store)["1"]
    assert e.source == "template" and e.priority == "low" and ai.calls == []


def test_low_priority_but_possibly_substantive_is_still_summarized():
    store, events = scan_changes(first=[obs()], second=[obs(body="new body text")])
    jev = FakeClassifier(classification("low", 0.9, substantive=0.4))
    ai = FakeSummarizer()
    assert enricher(jev, ai).enrich(events, store)["1"].source == "ai" and ai.calls


def test_unsure_jev_means_medium_priority_and_a_summary():
    store, events = scan_changes(first=[obs()], second=[obs(body="new body text")])
    jev = FakeClassifier(classification("low", 0.4, substantive=0.02))
    ai = FakeSummarizer()
    e = enricher(jev, ai).enrich(events, store)["1"]
    assert e.priority == "medium" and e.source == "ai"


def test_jev_down_means_no_priority_but_the_summary_still_happens():
    store, events = scan_changes(first=[obs()], second=[obs(body="new body text")])
    e = enricher(FakeClassifier(RuntimeError("boom")), FakeSummarizer()).enrich(events, store)["1"]
    assert e.priority is None and e.category is None and e.source == "ai"
    assert "classification unavailable (RuntimeError)" in e.note
    assert "classification unavailable" in store["1"].latest.classification["note"]


def test_openai_down_falls_back_to_a_template_for_updates():
    store, events = scan_changes(first=[obs()], second=[obs(body="new body text")])
    e = enricher(FakeClassifier(), FakeSummarizer(TimeoutError("slow"))).enrich(events, store)["1"]
    assert e.source == "template" and e.summary and e.priority == "high"
    assert "AI summary failed (TimeoutError)" in e.note


def test_nothing_configured_still_describes_updates_with_a_template():
    store, events = scan_changes(first=[obs()], second=[obs(body="new body text")])
    e = enricher().enrich(events, store)["1"]
    assert e.source == "template" and e.priority is None


def test_summary_limit_per_scan_switches_to_templates():
    store, events = scan_changes(
        first=[obs("1"), obs("2")], second=[obs("1", body="new one"), obs("2", body="new two")]
    )
    ai = FakeSummarizer()
    out = enricher(FakeClassifier(), ai, max_summaries_per_scan=1).enrich(events, store)
    assert sorted(e.source for e in out.values()) == ["ai", "template"]
    assert len(ai.calls) == 1
    assert any("limit" in (e.note or "") for e in out.values())


def test_files_are_part_of_the_change_text():
    old = FileContent("a.pdf", "1 KB", "s1", "version one text")
    new = FileContent("a.pdf", "1 KB", "s2", "version two text")
    store, events = scan_changes(first=[obs(files=[old])], second=[obs(files=[new])])
    ai = FakeSummarizer()
    enricher(FakeClassifier(), ai).enrich(events, store)
    assert "FILE REPLACED: a.pdf" in ai.calls[0][1] and "[one] WITH [two]" in ai.calls[0][1]


def test_oversized_changes_are_cut_and_flagged():
    first = obs(body=" ".join(f"a{i}" for i in range(6000)))
    second = obs(body=" ".join(f"b{i}" for i in range(6000)))
    store, events = scan_changes(first=[first], second=[second])
    ai = FakeSummarizer()
    enricher(FakeClassifier(), ai).enrich(events, store)
    _, text, truncated = ai.calls[0]
    assert truncated is True and "[CUT" in text
    assert store["1"].latest.classification["input_truncated"] is True


# ---- new posts ------------------------------------------------------------------------------


def test_new_post_is_summarized_and_classified_without_the_substantive_question():
    store: dict = {}
    detect_changes(store, ScanInput([obs("1")], T0, is_first_scan=True))
    result = detect_changes(store, ScanInput([obs("1"), obs("2", title="Brand new form")], T0))
    jev, ai = FakeClassifier(classification("high", 0.9, "application_form")), FakeSummarizer("A form.")
    out = enricher(jev, ai).enrich(result.events, store)

    assert set(out) == {"2"}
    assert (out["2"].summary, out["2"].category, out["2"].priority) == ("A form.", "application_form", "high")
    assert jev.calls[0][1] is False and ai.calls[0][0] == "new"
    assert "Brand new form" in ai.calls[0][1]


def test_new_post_without_ai_has_no_summary():
    store: dict = {}
    detect_changes(store, ScanInput([obs("1")], T0, is_first_scan=True))
    result = detect_changes(store, ScanInput([obs("1"), obs("2")], T0))
    e = enricher().enrich(result.events, store)["2"]
    assert e.summary is None and e.source == "none"


def test_new_post_when_openai_fails_keeps_the_card_without_a_summary():
    store: dict = {}
    detect_changes(store, ScanInput([obs("1")], T0, is_first_scan=True))
    result = detect_changes(store, ScanInput([obs("1"), obs("2")], T0))
    e = enricher(FakeClassifier(), FakeSummarizer(RuntimeError("x"))).enrich(result.events, store)["2"]
    assert e.summary is None and e.source == "none" and e.priority == "high"


# ---- events that need no AI -----------------------------------------------------------------


def test_removed_and_reappeared_events_are_left_alone():
    store: dict = {}
    detect_changes(store, ScanInput([obs("1"), obs("2")], T0, is_first_scan=True))
    detect_changes(store, ScanInput([obs("1")], T0 + timedelta(days=1)))
    removed = detect_changes(store, ScanInput([obs("1")], T0 + timedelta(days=2)))
    back = detect_changes(store, ScanInput([obs("1"), obs("2")], T0 + timedelta(days=3)))
    jev, ai = FakeClassifier(), FakeSummarizer()
    assert enricher(jev, ai).enrich(removed.events + back.events, store) == {}
    assert jev.calls == [] and ai.calls == []


@pytest.mark.parametrize("active", [(None, None, False), (FakeClassifier(), None, True)])
def test_active_flag(active):
    jev, ai, expected = active
    assert Enricher(jev, ai).active is expected
