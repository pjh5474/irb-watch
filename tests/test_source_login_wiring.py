from datetime import UTC, datetime

import httpx
import pytest

from irb_watch.crawlers.credentials import Credentials
from irb_watch.crawlers.errors import LoginExpiredError, LoginRejectedError
from irb_watch.crawlers.snuh_reader import SnuhSourceReader
from irb_watch.services.errors import ValidationError
from irb_watch.services.scan import ScanService
from irb_watch.services.source_login import cookies_for_source
from irb_watch.services.sources import SourceService
from tests.fakes import FakeAudit
from tests.fakes_scan import (
    FakeFileStorage,
    FakeReader,
    FakeScanRepo,
    FakeSourceStore,
    MemCreds,
    make_source,
    scan_once,
)
from tests.test_snuh_adapter import detail_html, list_html, row

URL = "https://hrpp.snuh.org/hrpp/bbs/list.do?key=2605277031"


# ---- source config ---------------------------------------------------------------------


def make_sources():
    return SourceService(FakeSourceStore(), FakeAudit(), lambda url: url)


def test_create_with_login_user_stores_it_in_config():
    source = make_sources().create("a", "Private", URL, login_user=" owner ")
    assert source.config == {"board_key": "2605277031", "login_user": "owner"}


def test_create_rejects_unsafe_login_user():
    with pytest.raises(ValidationError):
        make_sources().create("a", "Private", URL, login_user="../evil")


def test_update_sets_changes_and_clears_login_user():
    service = make_sources()
    source = service.create("a", "Private", URL)
    assert "login_user" not in source.config
    updated = service.update("a", source.id, login_user="owner")
    assert updated.config == {"board_key": "2605277031", "login_user": "owner"}
    cleared = service.update("a", source.id, login_user="")
    assert cleared.config == {"board_key": "2605277031"}
    untouched = service.update("a", source.id, name="Renamed")
    assert untouched.config == {"board_key": "2605277031"}


def test_login_user_makes_the_source_members_only_and_removing_it_never_makes_it_public():
    service = make_sources()
    public = service.create("a", "Public", "https://hrpp.snuh.org/hrpp/bbs/list.do?key=1")
    assert public.requires_login is False

    private = service.create("a", "Private", URL, login_user="owner")
    assert private.requires_login is True

    later = service.update("a", public.id, login_user="owner")
    assert later.requires_login is True

    cleared = service.update("a", private.id, login_user="")
    assert "login_user" not in cleared.config
    assert cleared.requires_login is True  # data already scanned stays protected


# ---- cookies_for_source ------------------------------------------------------------------


def test_public_source_does_not_log_in():
    calls = []
    source = make_source(1, config={"board_key": "1"})
    assert cookies_for_source(source, MemCreds(), login=lambda *a: calls.append(a)) is None
    assert calls == []


def test_login_source_logs_in_once_with_the_derived_site_name():
    calls = []

    def fake_login(cred_store, session_store, user, site, url):
        calls.append((user, site, url))
        return httpx.Cookies()

    source = make_source(1, config={"board_key": "2605277031", "login_user": "owner"})
    cookies = cookies_for_source(source, MemCreds(Credentials("i", "p")), login=fake_login)
    assert cookies is not None
    assert len(calls) == 1
    user, site, url = calls[0]
    assert (user, site) == ("owner", "snuh-2605277031")
    assert url.startswith("https://hrpp.snuh.org/hrpp/lgn/form.do?returnUrl=")


def test_login_source_without_stored_credentials_is_rejected_with_reason():
    source = make_source(1, config={"board_key": "1", "login_user": "owner"})
    with pytest.raises(LoginRejectedError, match="no stored credentials"):
        cookies_for_source(source, MemCreds(None))


# ---- failures stay with this source and show a reason --------------------------------------


@pytest.mark.parametrize(
    "error", [LoginRejectedError("login rejected: test"), LoginExpiredError("session died")]
)
def test_login_failures_mark_only_this_source_needs_review_with_reason(error):
    repo = FakeScanRepo()
    repo.add_source(make_source(1))
    repo.add_source(make_source(2))
    audit = FakeAudit()
    readers = {1: FakeReader(error=error), 2: FakeReader()}
    service = ScanService(
        repo,
        FakeFileStorage(),
        lambda source: readers[source.id],
        audit,
        clock=lambda: datetime(2026, 11, 6, tzinfo=UTC),
    )
    bad = service.scan_source(1, "admin-1")
    good = service.scan_source(2, "admin-1")
    assert bad.status == "needs_review" and bad.error == str(error)
    assert good.status == "ok"
    assert repo.get_source(1).last_scan_status == "needs_review"
    assert repo.get_source(1).last_scan_error == str(error)


def test_factory_login_error_is_also_caught():
    def factory(source):
        raise LoginRejectedError("login rejected: no stored credentials")

    result, _ = scan_once(factory=factory)
    assert result.status == "needs_review" and "no stored credentials" in result.error


def test_session_dying_mid_scan_fails_the_scan_not_every_post():
    """A login page on the 2nd post must not be recorded as one broken post per post."""
    from irb_watch.crawlers.fetcher import SafeFetcher
    from irb_watch.crawlers.snuh import SnuhAdapter
    from tests.test_snuh_adapter import RESOLVER

    def handler(request):
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if path == "/hrpp/bbs/list.do":
            return httpx.Response(200, text=list_html([row("1", "A"), row("2", "B")]))
        if path == "/hrpp/lgn/form.do":
            return httpx.Response(200, text='<input type="password" name="userPswd">')
        if request.url.params.get("pstSn") == "1":
            return httpx.Response(200, text=detail_html(files=[]))
        return httpx.Response(302, headers={"location": "/hrpp/lgn/form.do"})

    fetcher = SafeFetcher(
        transport=httpx.MockTransport(handler), resolver=RESOLVER, delay=0, sleep=lambda s: None
    )
    reader = SnuhSourceReader(SnuhAdapter(fetcher), "2605277031", 200, 1_000_000)
    with pytest.raises(LoginExpiredError):
        reader.read(lambda sha, name, content: sha)
