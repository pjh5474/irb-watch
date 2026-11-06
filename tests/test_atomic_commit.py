"""The scan is saved by one call (db function commit_scan); these tests cover the Python side.

The all-or-nothing behaviour itself is a Postgres transaction and can only be proven on the
real project (see docs/runbook.md); here a fake stands in.
"""

import json
from datetime import UTC, datetime

import pytest

from irb_watch.crawlers.errors import ScanTooLargeError
from irb_watch.db.documents import SupabaseScanRepo, build_commit_documents
from tests.fakes_scan import FakeScanRepo, make_obs
from tests.test_scan_service import Harness

NOW = datetime(2026, 11, 6, tzinfo=UTC)


class CapturingRepo(FakeScanRepo):
    """Records what the Supabase repo would send, then behaves as the fake."""

    def commit_scan(self, source_id, loaded, seen, now, cards, status, error):
        self.sent = build_commit_documents(loaded)
        self.sent_cards = list(cards)
        return super().commit_scan(source_id, loaded, seen, now, cards, status, error)


def test_payload_sends_only_what_changed():
    h = Harness(repo_class=CapturingRepo)
    h.scan(make_obs("1", body="aaa"), make_obs("2", body="bbb"), day=0)
    first = h.repo.sent
    assert [d["external_id"] for d in first] == ["1", "2"]
    assert all(d["doc_id"] is None and len(d["versions"]) == 1 for d in first)
    assert first[0]["versions"][0]["change_type"] == "baseline"

    h.scan(make_obs("1", body="aaa"), make_obs("2", body="bbb"), day=1)
    assert h.repo.sent == []  # unchanged: only last_seen_at moves, inside the database function

    h.scan(make_obs("1", body="aaa"), make_obs("2", body="bbb changed"), day=2)
    (changed,) = h.repo.sent
    assert changed["external_id"] == "2" and changed["doc_id"] is not None
    assert [v["version_no"] for v in changed["versions"]] == [2]


def test_payload_is_plain_json():
    h = Harness(repo_class=CapturingRepo)
    h.scan(make_obs("1", body="aaa"), day=0)
    json.dumps(h.repo.sent, ensure_ascii=False)


def test_cards_travel_with_the_data_and_scan_finished_is_last():
    h = Harness(repo_class=CapturingRepo)
    h.scan(make_obs("1", body="aaa"), day=0)
    h.scan(make_obs("1", body="aaa changed"), day=1)
    assert [c.event_type for c in h.repo.sent_cards] == ["document.updated", "scan.finished"]
    assert h.repo.sent_cards[0].external_id == "1"
    assert h.repo.sent_cards[1].external_id is None


def test_failed_save_leaves_no_data_cards_or_baseline_marker():
    h = Harness()
    h.repo.fail_persist = True
    result = h.scan(make_obs("1"), day=0)
    assert result.status == "failed" and "database down" in result.error
    assert h.repo.get_source(1).last_scan_at is None
    assert h.repo.history == {}
    assert h.events("document.") == []
    assert "scan.finished" not in h.events("scan.")  # only the failure record exists


def test_too_large_scan_is_needs_review_with_a_reason():
    class TooBig(FakeScanRepo):
        def commit_scan(self, *args):
            raise ScanTooLargeError("scan too large to save safely in one step (25.0 MB)")

    h = Harness(repo_class=TooBig)
    result = h.scan(make_obs("1"), day=0)
    assert result.status == "needs_review" and "too large" in result.error
    assert h.repo.get_source(1).last_scan_at is None


# ---- SupabaseScanRepo: size cap and call shape ------------------------------------


class FakeClient:
    def __init__(self):
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return type("Request", (), {"execute": lambda self: type("R", (), {"data": {"1": 7}})()})()


def _loaded_with_one_post():
    h = Harness()
    h.scan(make_obs("1", body="x" * 100), day=0)
    return h.repo.load_store(1)


def test_supabase_repo_sends_one_rpc_with_named_params():
    client = FakeClient()
    ids = SupabaseScanRepo(client).commit_scan(
        1, _loaded_with_one_post(), {"1"}, NOW, [], "ok", None
    )
    assert ids == {"1": 7}
    ((name, params),) = client.calls
    assert name == "commit_scan"
    assert set(params) == {
        "p_source_id", "p_now", "p_documents", "p_seen", "p_cards", "p_status", "p_error",
    }
    assert params["p_seen"] == ["1"] and params["p_status"] == "ok"


def test_supabase_repo_refuses_oversized_payload_before_calling_the_database():
    client = FakeClient()
    repo = SupabaseScanRepo(client, max_payload_bytes=50)
    with pytest.raises(ScanTooLargeError, match="nothing was saved"):
        repo.commit_scan(1, _loaded_with_one_post(), {"1"}, NOW, [], "ok", None)
    assert client.calls == []
