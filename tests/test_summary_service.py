from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import get_summary_service, get_verifier
from irb_watch.core.state import FileContent, VersionContent
from irb_watch.main import app
from irb_watch.services.errors import (
    ForbiddenError,
    NotFoundError,
    TooManyRequestsError,
    UnavailableError,
    UpstreamError,
)
from irb_watch.services.summaries import DocumentForSummary, SummaryService
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_ai import FakeSummarizer
from tests.test_admin_users_api import FakeVerifier

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


class FakeSummaryRepo:
    def __init__(self):
        self.docs = {
            10: DocumentForSummary(
                10, 1, 100, 2,
                VersionContent("Form", "page text", (FileContent("a.pdf", "1 KB", "s", "file text"),)),
                None,
            )
        }
        self.subscriptions = {("user-1", 1)}
        self.requests: list[tuple[str, datetime]] = []
        self.saved: dict[int, str] = {}

    def get_latest(self, document_id):
        return self.docs.get(document_id)

    def is_subscribed(self, user_id, source_id):
        return (user_id, source_id) in self.subscriptions

    def count_requests_since(self, user_id, since):
        return sum(1 for u, at in self.requests if u == user_id and at >= since)

    def save_summary(self, version_id, text, now):
        self.saved[version_id] = text
        d = self.docs[10]
        self.docs[10] = DocumentForSummary(
            d.document_id, d.source_id, d.version_id, d.version_no, d.content, text
        )

    def record_request(self, user_id, version_id, now):
        self.requests.append((user_id, now))


@pytest.fixture
def env():
    users = FakeStore()
    users.add("user-1", "u@example.com")
    users.add("other", "o@example.com")
    users.add("admin-1", "a@example.com", role="admin")
    users.add("gone", "g@example.com", deleted_at="2026-01-01T00:00:00+00:00")
    repo, audit, ai = FakeSummaryRepo(), FakeAudit(), FakeSummarizer("Whole document summary.")
    service = SummaryService(users, repo, ai, audit, daily_limit=2, clock=lambda: NOW)
    return service, repo, audit, ai, users


def test_subscriber_gets_a_fresh_summary_then_a_cached_one(env):
    service, repo, audit, ai, _ = env
    first = service.summarize_document("user-1", 10)
    assert (first.summary, first.cached, first.version_no) == ("Whole document summary.", False, 2)
    assert repo.saved[100] == "Whole document summary."
    assert audit.entries[-1]["event_type"] == "summary.requested"

    second = service.summarize_document("user-1", 10)
    assert second.cached is True and len(ai.calls) == 1 and len(repo.requests) == 1


def test_the_whole_document_text_is_sent(env):
    service, _, _, ai, _ = env
    service.summarize_document("user-1", 10)
    kind, text, truncated = ai.calls[0]
    assert kind == "document" and "page text" in text and "file text" in text and not truncated


def test_non_subscriber_sees_not_found(env):
    service, *_ = env
    with pytest.raises(NotFoundError):
        service.summarize_document("other", 10)


def test_admin_may_summarize_any_document(env):
    service, *_ = env
    assert service.summarize_document("admin-1", 10).summary


def test_unknown_document(env):
    service, *_ = env
    with pytest.raises(NotFoundError):
        service.summarize_document("user-1", 999)


@pytest.mark.parametrize("user", ["gone", "ghost"])
def test_deleted_or_unknown_accounts_are_refused(env, user):
    service, *_ = env
    with pytest.raises(ForbiddenError):
        service.summarize_document(user, 10)


def test_daily_limit_counts_only_new_summaries_in_the_last_day(env):
    service, repo, *_ = env
    repo.requests = [("user-1", NOW - timedelta(hours=1)), ("user-1", NOW - timedelta(hours=2))]
    with pytest.raises(TooManyRequestsError, match="daily limit"):
        service.summarize_document("user-1", 10)
    repo.requests = [("user-1", NOW - timedelta(days=2)), ("user-1", NOW - timedelta(hours=1))]
    assert service.summarize_document("user-1", 10).cached is False


def test_cached_summaries_ignore_the_limit(env):
    service, repo, *_ = env
    service.summarize_document("user-1", 10)
    repo.requests = [("user-1", NOW)] * 5
    assert service.summarize_document("user-1", 10).cached is True


def test_failed_generation_is_not_saved_or_counted(env):
    service, repo, audit, ai, _ = env
    ai.result = TimeoutError("slow")
    with pytest.raises(UpstreamError, match="TimeoutError"):
        service.summarize_document("user-1", 10)
    assert repo.saved == {} and repo.requests == [] and audit.entries == []


def test_switched_off_when_no_summarizer(env):
    _, repo, audit, _, users = env
    service = SummaryService(users, repo, None, audit, 2, clock=lambda: NOW)
    with pytest.raises(UnavailableError):
        service.summarize_document("user-1", 10)


# ---- over HTTP --------------------------------------------------------------------------


@pytest.fixture
def client(env):
    service = env[0]
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_summary_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def auth(user):
    return {"Authorization": f"Bearer tok-{user}"}


def test_endpoint_needs_a_login(client):
    assert client.post("/documents/10/summary").status_code == 401


def test_endpoint_returns_the_summary_then_the_cached_copy(client):
    first = client.post("/documents/10/summary", headers=auth("user-1"))
    assert first.status_code == 200
    assert first.json() == {"summary": "Whole document summary.", "cached": False, "version_no": 2}
    assert client.post("/documents/10/summary", headers=auth("user-1")).json()["cached"] is True


@pytest.mark.parametrize(
    "user, document, status",
    [("other", 10, 404), ("user-1", 999, 404), ("gone", 10, 403)],
)
def test_endpoint_status_codes(client, user, document, status):
    assert client.post(f"/documents/{document}/summary", headers=auth(user)).status_code == status


def test_endpoint_rate_limit_is_429(client, env):
    _, repo, *_ = env
    repo.requests = [("user-1", NOW)] * 2
    assert client.post("/documents/10/summary", headers=auth("user-1")).status_code == 429
