from datetime import UTC, datetime, timedelta

from irb_watch.core import health as h

NOW = datetime(2026, 11, 10, tzinfo=UTC)


def hours_ago(n: float) -> str:
    return (NOW - timedelta(hours=n)).isoformat()


def health(**kw):
    base = {"is_active": True, "last_scan_at": hours_ago(2), "last_scan_status": "ok"}
    return h.source_health(now=NOW, **{**base, **kw})


# The same cases as the frontend's tests/source-health.test.ts: the admin light and the mails
# must agree about what is amber and what is red.


def test_a_recent_good_scan_is_ok():
    assert health() == h.Health(h.OK)


def test_inactive_wins_over_everything():
    assert health(is_active=False, last_scan_status="failed").level == h.INACTIVE


def test_a_running_scan_is_scanning_and_not_an_alarm():
    result = health(scan_in_progress=True, last_scan_status="failed")
    assert result.level == h.SCANNING and not result.alarming


def test_paused_after_a_rejected_login_is_a_problem():
    assert health(scans_paused_reason="login rejected") == h.Health(h.PROBLEM, h.PAUSED)


def test_needs_review_is_a_problem():
    assert health(last_scan_status="needs_review") == h.Health(h.PROBLEM, h.NEEDS_REVIEW)


def test_a_failed_scan_is_attention():
    assert health(last_scan_status="failed") == h.Health(h.ATTENTION, h.FAILED)


def test_no_good_scan_for_longer_than_the_limit_is_attention():
    assert health(last_scan_at=hours_ago(35)).level == h.OK
    assert health(last_scan_at=hours_ago(37)) == h.Health(h.ATTENTION, h.STALE)
    assert health(last_scan_at=hours_ago(30), stale_hours=24).reason == h.STALE


def test_never_scanned_is_idle_not_an_alarm():
    result = health(last_scan_at=None, last_scan_status=None)
    assert result.level == h.IDLE and not result.alarming


def test_an_unreadable_date_is_never_ok():
    assert health(last_scan_at="not a date") == h.Health(h.ATTENTION, h.STALE)


def test_a_date_without_a_timezone_is_read_as_utc():
    naive = (NOW - timedelta(hours=2)).replace(tzinfo=None).isoformat()
    assert health(last_scan_at=naive).level == h.OK


def test_only_attention_and_problem_are_alarms_and_problem_is_worse():
    assert [health(last_scan_status=s).alarming for s in ("ok", "failed", "needs_review")] == [
        False,
        True,
        True,
    ]
    assert h.RANK[h.PROBLEM] > h.RANK[h.ATTENTION] > h.RANK[h.OK]
