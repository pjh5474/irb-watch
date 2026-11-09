import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import (
    get_alert_service,
    get_scan_service,
    get_source_service,
    get_user_store,
    get_verifier,
)
from irb_watch.main import app
from irb_watch.services.errors import ConflictError, NotFoundError
from irb_watch.services.sources import SourceService
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_scan import FakeSourceStore, make_source
from tests.test_admin_users_api import FakeVerifier

URL = "https://hrpp.snuh.org/hrpp/bbs/list.do?key=2605277029"


class StubAlerts:
    """Stands in for the mail alarms: records which sources were looked at after a scan."""

    def __init__(self):
        self.evaluated: list[int] = []
        self.error: Exception | None = None

    def evaluate_source(self, source_id):
        if self.error:
            raise self.error
        self.evaluated.append(source_id)


class StubScanService:
    def __init__(self):
        self.begun: list[int] = []
        self.ran: list[tuple] = []
        self.error: Exception | None = None

    def begin_scan(self, source_id):
        if self.error:
            raise self.error
        self.begun.append(source_id)
        return make_source(source_id)

    def run_scan(self, source, actor_id):
        self.ran.append((source.id, actor_id))


@pytest.fixture
def api():
    users = FakeStore()
    users.add("admin-1", "boss@example.com", role="admin")
    users.add("user-1", "plain@example.com")
    sources = SourceService(FakeSourceStore(), FakeAudit(), lambda url: url)
    scans = StubScanService()
    scans.alerts = StubAlerts()
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: users
    app.dependency_overrides[get_source_service] = lambda: sources
    app.dependency_overrides[get_scan_service] = lambda: scans
    app.dependency_overrides[get_alert_service] = lambda: scans.alerts
    yield TestClient(app), scans
    app.dependency_overrides.clear()


ADMIN = {"Authorization": "Bearer tok-admin-1"}
USER = {"Authorization": "Bearer tok-user-1"}


def test_regular_users_cannot_manage_sources(api):
    client, _ = api
    assert client.get("/admin/sources", headers=USER).status_code == 403
    assert client.post("/admin/sources", headers=USER, json={"name": "x", "url": URL}).status_code == 403
    assert client.post("/admin/sources/1/scan", headers=USER).status_code == 403
    assert client.get("/admin/sources").status_code == 401


def test_create_and_list(api):
    client, _ = api
    r = client.post("/admin/sources", headers=ADMIN, json={"name": "Rules", "url": URL})
    assert r.status_code == 201
    body = r.json()
    assert body["adapter"] == "snuh_hrpp" and body["url"] == URL and body["scan_in_progress"] is False
    assert [s["name"] for s in client.get("/admin/sources", headers=ADMIN).json()] == ["Rules"]


def test_create_duplicate_and_unsupported(api):
    client, _ = api
    client.post("/admin/sources", headers=ADMIN, json={"name": "Rules", "url": URL})
    assert client.post("/admin/sources", headers=ADMIN, json={"name": "B", "url": URL}).status_code == 409
    bad = client.post("/admin/sources", headers=ADMIN, json={"name": "B", "url": "https://example.com/x"})
    assert bad.status_code == 422


def test_update(api):
    client, _ = api
    client.post("/admin/sources", headers=ADMIN, json={"name": "Rules", "url": URL})
    r = client.patch("/admin/sources/1", headers=ADMIN, json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert client.patch("/admin/sources/9", headers=ADMIN, json={"name": "x"}).status_code == 404


def test_scan_returns_202_and_runs_in_the_background(api):
    client, scans = api
    r = client.post("/admin/sources/1/scan", headers=ADMIN)
    assert r.status_code == 202 and r.json() == {"status": "started", "source_id": 1}
    assert scans.begun == [1] and scans.ran == [(1, "admin-1")]


def test_after_a_manual_scan_the_source_is_checked_for_a_mail_alarm(api):
    client, scans = api
    client.post("/admin/sources/1/scan", headers=ADMIN)
    assert scans.alerts.evaluated == [1]


def test_a_failing_alarm_check_never_turns_a_good_scan_into_an_error(api):
    client, scans = api
    scans.alerts.error = RuntimeError("mail service exploded")
    r = client.post("/admin/sources/1/scan", headers=ADMIN)
    assert r.status_code == 202 and scans.ran == [(1, "admin-1")]


@pytest.mark.parametrize("error, status", [(NotFoundError("no"), 404), (ConflictError("busy"), 409)])
def test_scan_refusals_happen_before_anything_starts(api, error, status):
    client, scans = api
    scans.error = error
    assert client.post("/admin/sources/1/scan", headers=ADMIN).status_code == status
    assert scans.ran == []
