from datetime import UTC, datetime, timedelta

import pytest

from irb_watch.core.detect import Observation, ScanInput, detect_changes
from irb_watch.core.state import CURRENT, NEEDS_REVIEW, REMOVED, FileContent, VersionContent

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def clock(n):
    return T0 + timedelta(days=n)


def file(name="a.pdf", sha="s1", text="file text here"):
    return FileContent(name, "10 KB", sha, text)


def obs(external_id="1", title="Title", body="body", files=()):
    return Observation(external_id, f"http://site/{external_id}", "2026.09.03",
                       VersionContent(title, body, tuple(files)))


def scan(observations, day, **kw):
    return ScanInput(observations=list(observations), now=clock(day), **kw)


@pytest.fixture
def store():
    return {}


def baseline(store, *observations):
    return detect_changes(store, scan(observations, 0, is_first_scan=True))


# ---- baseline ----------------------------------------------------------------------


def test_first_scan_is_a_silent_baseline(store):
    result = baseline(store, obs("1"), obs("2"))
    assert result.events == [] and result.baseline is True
    assert set(store) == {"1", "2"}
    assert store["1"].latest.change_type == "baseline" and store["1"].latest.version_no == 1


def test_unchanged_rescan_produces_nothing(store):
    baseline(store, obs("1", files=[file()]))
    result = detect_changes(store, scan([obs("1", files=[file()])], 1))
    assert result.events == [] and len(store["1"].versions) == 1


# ---- new ----------------------------------------------------------------------------


def test_new_post_after_baseline_is_an_event(store):
    baseline(store, obs("1"))
    result = detect_changes(store, scan([obs("1"), obs("2", title="Fresh")], 1))
    assert [(e.kind, e.external_id, e.title) for e in result.events] == [("new", "2", "Fresh")]
    assert store["2"].latest.change_type == "new"


# ---- updated ------------------------------------------------------------------------


def test_title_change_is_an_update_with_low_priority(store):
    baseline(store, obs("1", title="Old"))
    result = detect_changes(store, scan([obs("1", title="New")], 1))
    [event] = result.events
    assert event.kind == "updated" and event.version_no == 2
    assert event.detail.title_change == ("Old", "New") and event.detail.suggested_priority == "low"
    assert store["1"].latest.change_type == "updated"


def test_body_change_keeps_a_diff_users_can_view(store):
    baseline(store, obs("1", body="revised on August"))
    result = detect_changes(store, scan([obs("1", body="revised on September")], 1))
    detail = result.events[0].detail
    assert detail.body_diff.changed
    assert [c.new for c in detail.body_diff.changes if c.op == "replace"] == ["September"]


def test_replaced_file_is_an_update(store):
    baseline(store, obs("1", files=[file(sha="s1", text="v1")]))
    result = detect_changes(store, scan([obs("1", files=[file(sha="s2", text="v2")])], 1))
    assert result.events[0].detail.file_changes[0].kind == "replaced"
    assert len(store["1"].versions) == 2


def test_resaved_file_is_recorded_silently(store):
    baseline(store, obs("1", files=[file(sha="s1", text="same")]))
    result = detect_changes(store, scan([obs("1", files=[file(sha="s2", text="same")])], 1))
    assert result.events == []
    assert store["1"].latest.change_type == "resaved" and store["1"].latest.version_no == 2
    # The stored hash is current now, so the next scan does not see the same change again.
    again = detect_changes(store, scan([obs("1", files=[file(sha="s2", text="same")])], 2))
    assert again.events == [] and len(store["1"].versions) == 2


def test_version_history_supports_date_lookup(store):
    baseline(store, obs("1", body="one"))
    detect_changes(store, scan([obs("1", body="two")], 10))
    assert store["1"].version_as_of(clock(5)).content.body_text == "one"
    assert store["1"].version_as_of(clock(10)).content.body_text == "two"


# ---- removal (2 misses) ---------------------------------------------------------------


def test_removed_only_after_two_consecutive_misses(store):
    baseline(store, obs("1"), obs("2"))
    first = detect_changes(store, scan([obs("1")], 1))
    assert first.events == [] and store["2"].miss_count == 1 and store["2"].status == CURRENT
    second = detect_changes(store, scan([obs("1")], 2))
    assert [(e.kind, e.external_id) for e in second.events] == [("removed", "2")]
    assert store["2"].status == REMOVED and store["2"].removed_at == clock(2)


def test_a_sighting_resets_the_miss_counter(store):
    baseline(store, obs("1"), obs("2"))
    detect_changes(store, scan([obs("1")], 1))
    detect_changes(store, scan([obs("1"), obs("2")], 2))
    assert store["2"].miss_count == 0
    result = detect_changes(store, scan([obs("1")], 3))
    assert result.events == [] and store["2"].miss_count == 1


def test_removed_is_reported_once(store):
    baseline(store, obs("1"), obs("2"))
    for day in (1, 2, 3, 4):
        detect_changes(store, scan([obs("1")], day))
    assert store["2"].status == REMOVED
    third = detect_changes(store, scan([obs("1")], 5))
    assert third.events == []


def test_removed_post_that_returns_is_reappeared(store):
    baseline(store, obs("1"), obs("2", body="same"))
    detect_changes(store, scan([obs("1")], 1))
    detect_changes(store, scan([obs("1")], 2))
    result = detect_changes(store, scan([obs("1"), obs("2", body="same")], 3))
    assert [e.kind for e in result.events] == ["reappeared"]
    assert store["2"].status == CURRENT and store["2"].removed_at is None


def test_reappeared_with_changes_reports_both(store):
    baseline(store, obs("1"), obs("2", body="old"))
    detect_changes(store, scan([obs("1")], 1))
    detect_changes(store, scan([obs("1")], 2))
    result = detect_changes(store, scan([obs("1"), obs("2", body="new")], 3))
    assert [e.kind for e in result.events] == ["updated", "reappeared"]


def test_incomplete_scan_never_counts_misses(store):
    baseline(store, obs("1"), obs("2"))
    for day in (1, 2, 3):
        result = detect_changes(store, scan([obs("1")], day, complete=False))
        assert result.events == []
    assert store["2"].miss_count == 0 and store["2"].status == CURRENT


# ---- failures and unreadable posts ----------------------------------------------------


def test_failed_post_is_needs_review_and_keeps_old_state(store):
    baseline(store, obs("1", body="good"))
    result = detect_changes(store, scan([], 1, failures={"1": "detail page layout changed"}))
    assert result.needs_review == {"1": "detail page layout changed"}
    assert result.events == []
    assert store["1"].status == NEEDS_REVIEW and store["1"].miss_count == 0
    assert store["1"].latest.content.body_text == "good"


def test_failures_never_lead_to_removal(store):
    baseline(store, obs("1"))
    for day in (1, 2, 3, 4):
        detect_changes(store, scan([], day, failures={"1": "fetch error"}))
    assert store["1"].status == NEEDS_REVIEW and store["1"].miss_count == 0


def test_post_recovers_from_needs_review(store):
    baseline(store, obs("1", body="same"))
    detect_changes(store, scan([], 1, failures={"1": "boom"}))
    result = detect_changes(store, scan([obs("1", body="same")], 2))
    assert result.events == [] and store["1"].status == CURRENT


def test_failure_for_unknown_post_is_reported_without_creating_it(store):
    baseline(store, obs("1"))
    result = detect_changes(store, scan([obs("1")], 1, failures={"9": "layout changed"}))
    assert result.needs_review == {"9": "layout changed"} and "9" not in store


def test_unreadable_posts_are_neither_seen_nor_missing(store):
    baseline(store, obs("1"), obs("2"))
    for day in (1, 2, 3):
        result = detect_changes(
            store, scan([obs("1")], day, unreadable_ids=frozenset({"2"}))
        )
        assert result.events == []
    assert store["2"].status == CURRENT and store["2"].miss_count == 0


def test_duplicate_ids_in_one_scan_are_rejected(store):
    with pytest.raises(ValueError, match="duplicate"):
        detect_changes(store, scan([obs("1"), obs("1")], 0))
