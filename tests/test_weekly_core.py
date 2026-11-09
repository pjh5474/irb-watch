from datetime import UTC, datetime, timedelta

from irb_watch.core import weekly as w

NOW = datetime(2026, 11, 9, 7, 0, tzinfo=UTC)
WIN = w.Window.last_days(NOW)


def at(days_ago: float) -> datetime:
    return NOW - timedelta(days=days_ago)


def ev(kind, days_ago=1, actor=None, source=None, **payload):
    return w.Event(kind, actor, source, at(days_ago), payload)


def user(uid, email, signed=None, created=None):
    return w.UserInfo(uid, email, at(created) if created is not None else None, at(signed) if signed is not None else None)


# ---- the window ---------------------------------------------------------------------------


def test_the_window_is_half_open_and_the_previous_one_ends_where_it_starts():
    assert WIN.contains(WIN.start) and not WIN.contains(WIN.end)
    assert not WIN.contains(WIN.start - timedelta(microseconds=1))
    before = WIN.previous()
    assert before.end == WIN.start and before.end - before.start == timedelta(days=7)
    assert not WIN.contains(None)


# ---- period figures ------------------------------------------------------------------------


def test_summaries_downloads_and_documents_are_counted_only_inside_the_window():
    events = [
        ev("summary.requested", 1), ev("summary.regenerated", 2), ev("summary.requested", 8),
        ev("file.download_link", 1, source=4), ev("file.download_link", 3, source=4), ev("file.download_link", 3, source=5),
        ev("file.download_link", 9, source=4),
        ev("document.new", 1, source=4), ev("document.reappeared", 2, source=4), ev("document.updated", 2, source=4),
        ev("document.updated", 10, source=4),
    ]
    p = w.period(WIN, events)
    assert (p.summaries, p.downloads) == (2, 3)
    assert p.downloads_by_source == {4: 2, 5: 1}
    assert p.docs_by_source == {4: w.DocCounts(new=2, updated=1)}
    before = w.period(WIN.previous(), events)
    assert (before.summaries, before.downloads) == (1, 1)


def test_an_event_exactly_on_the_edges():
    start = w.Event("file.download_link", None, None, WIN.start)
    end = w.Event("file.download_link", None, None, WIN.end)
    assert w.period(WIN, [start, end]).downloads == 1  # start is in, end is out
    assert w.period(WIN.previous(), [start, end]).downloads == 0


def test_scan_health_sums_the_run_payloads_and_ignores_garbage():
    events = [
        ev("scan.run_finished", 1, scanned=3, failed=1, skipped=0),
        ev("scan.run_finished", 2, scanned=3, failed=0, skipped=1),
        ev("scan.run_finished", 3, scanned="many", failed=-4, skipped=True),
        ev("scan.run_finished", 9, scanned=99, failed=99, skipped=99),  # outside the window
    ]
    p = w.period(WIN, events)
    assert (p.scan_runs, p.scans, p.scans_failed, p.scans_skipped) == (3, 6, 1, 1)


def test_an_empty_week_is_all_zeros():
    p = w.period(WIN, [])
    assert (p.summaries, p.downloads, p.scan_runs) == (0, 0, 0)
    assert p.docs_by_source == {} and p.downloads_by_source == {}


# ---- accounts ----------------------------------------------------------------------------------


def test_signed_in_and_new_accounts_count_dates_in_the_window():
    users = [user("a", "a@x", signed=1, created=30), user("b", "b@x", signed=10, created=2), user("c", "c@x")]
    assert w.signed_in(WIN, users) == 1
    assert w.new_accounts(WIN, users) == 1


# ---- the per-user table ----------------------------------------------------------------------


def test_only_users_with_activity_are_listed_sorted_by_use_then_last_sign_in():
    users = [
        user("quiet", "quiet@x", signed=20),
        user("heavy", "heavy@x", signed=3),
        user("light", "light@x", signed=1),
        user("only-in", "signedin@x", signed=2),
    ]
    events = [
        ev("summary.requested", 1, actor="heavy"), ev("summary.requested", 2, actor="heavy"),
        ev("file.download_link", 2, actor="heavy"), ev("file.download_link", 1, actor="light"),
    ]
    rows, hidden = w.user_rows(WIN, events, users)
    assert [r.email for r in rows] == ["heavy@x", "light@x", "signedin@x"]  # quiet@x has no activity
    assert (rows[0].summaries, rows[0].downloads) == (2, 1) and hidden == 0
    assert rows[2].summaries == 0 and rows[2].downloads == 0  # a sign-in alone counts as activity


def test_a_user_with_only_old_activity_is_not_listed():
    rows, _ = w.user_rows(WIN, [ev("summary.requested", 9, actor="a")], [user("a", "a@x", signed=9)])
    assert rows == []


def test_accounts_without_an_email_are_left_out():
    rows, _ = w.user_rows(WIN, [ev("summary.requested", 1, actor="a")], [user("a", None, signed=1)])
    assert rows == []


def test_ties_are_ordered_by_the_latest_sign_in_then_email():
    users = [user("a", "b@x", signed=3), user("b", "a@x", signed=3), user("c", "c@x", signed=1)]
    rows, _ = w.user_rows(WIN, [], users)
    assert [r.email for r in rows] == ["c@x", "a@x", "b@x"]


def test_the_table_is_capped_and_the_rest_is_a_count():
    users = [user(f"u{i}", f"u{i}@x", signed=1) for i in range(60)]
    rows, hidden = w.user_rows(WIN, [], users, cap=50)
    assert len(rows) == 50 and hidden == 10
