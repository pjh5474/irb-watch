import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from irb_watch.crawlers.credentials import Credentials
from irb_watch.crawlers.errors import FetchError, LoginRejectedError, SiteUnavailableError
from irb_watch.crawlers.fetcher import SafeFetcher
from irb_watch.crawlers.login import browser_login, snuh_login_url
from irb_watch.crawlers.snuh import SnuhAdapter
from irb_watch.crawlers.snuh_reader import SnuhSourceReader
from tests.fakes import FakeAudit
from tests.fakes_scan import FakeFileStorage, FakeReader, FakeScanRepo, make_source
from tests.test_snuh_adapter import KEY, RESOLVER, list_html, row

URL = "https://hrpp.snuh.org/x"


def fetcher_for(handler, sleeps, **kw):
    return SafeFetcher(
        transport=httpx.MockTransport(handler),
        resolver=RESOLVER,
        delay=0,
        sleep=sleeps.append,
        check_robots=kw.pop("check_robots", False),
        **kw,
    )


def sequence(*answers):
    """Handler answering with each item in turn: an int status, 'ok', or an exception."""
    calls = []
    items = list(answers)

    def handler(request):
        calls.append(request.url.path)
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        if item == "ok":
            return httpx.Response(200, text="fine")
        return httpx.Response(item, text="gateway")

    return handler, calls


# ---- request-level retries --------------------------------------------------------------


def test_502_then_success_waits_5_seconds_once():
    handler, calls = sequence(502, "ok")
    sleeps = []
    assert fetcher_for(handler, sleeps).get(URL).text == "fine"
    assert len(calls) == 2 and sleeps == [5.0]


def test_two_failures_then_success_waits_5_then_15():
    handler, calls = sequence(503, 504, "ok")
    sleeps = []
    assert fetcher_for(handler, sleeps).get(URL).status == 200
    assert len(calls) == 3 and sleeps == [5.0, 15.0]


def test_three_failures_give_up_with_the_reason_and_the_count():
    handler, calls = sequence(502)
    with pytest.raises(FetchError, match=r"HTTP 502.*gave up after 3 tries") as info:
        fetcher_for(handler, []).get(URL)
    assert len(calls) == 3
    assert not isinstance(info.value, SiteUnavailableError)  # budget not used up


@pytest.mark.parametrize("status", [500, 404, 403, 429])
def test_other_errors_are_not_retried(status):
    handler, calls = sequence(status)
    sleeps = []
    with pytest.raises(FetchError, match=f"HTTP {status}"):
        fetcher_for(handler, sleeps).get(URL)
    assert len(calls) == 1 and sleeps == []


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("cut")]
)
def test_connection_trouble_is_retried(error):
    handler, calls = sequence(error, "ok")
    assert fetcher_for(handler, []).get(URL).status == 200
    assert len(calls) == 2


def test_persistent_connection_trouble_reports_the_last_error():
    handler, _ = sequence(httpx.ConnectError("refused"))
    with pytest.raises(FetchError, match=r"refused.*gave up after 3 tries"):
        fetcher_for(handler, []).get(URL)


def test_custom_waits_set_the_number_of_tries():
    handler, calls = sequence(502)
    sleeps = []
    with pytest.raises(FetchError, match="gave up after 2 tries"):
        fetcher_for(handler, sleeps, retry_waits=(1.0,)).get(URL)
    assert len(calls) == 2 and sleeps == [1.0]


def test_robots_txt_502_is_retried_too():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(502) if calls.count("/robots.txt") == 1 else httpx.Response(
                200, text="User-agent: *\nAllow: /"
            )
        return httpx.Response(200, text="fine")

    sleeps = []
    assert fetcher_for(handler, sleeps, check_robots=True).get(URL).text == "fine"
    assert sleeps == [5.0]


# ---- per-scan retry budget ----------------------------------------------------------------


def test_budget_runs_out_across_requests_and_stops_the_scan():
    handler, calls = sequence(502)
    f = fetcher_for(handler, [], retry_budget=3)
    with pytest.raises(FetchError, match="gave up after 3 tries"):  # uses 2 retries
        f.get(URL)
    with pytest.raises(SiteUnavailableError, match=r"site unavailable.*after 3 retries"):
        f.get(URL)  # 1 retry left, then the budget is gone
    assert len(calls) == 3 + 2


def test_budget_is_per_fetcher_so_the_next_scan_starts_fresh():
    handler, _ = sequence(502)
    first = fetcher_for(handler, [], retry_budget=0)
    with pytest.raises(SiteUnavailableError):
        first.get(URL)
    handler2, _ = sequence("ok")
    assert fetcher_for(handler2, [], retry_budget=0).get(URL).status == 200


def test_site_down_aborts_the_whole_read_once_not_one_flag_per_post():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.path == "/hrpp/bbs/list.do":
            return httpx.Response(200, text=list_html([row(str(i), f"P{i}") for i in range(1, 6)]))
        return httpx.Response(502)

    sleeps = []
    fetcher = SafeFetcher(
        transport=httpx.MockTransport(handler),
        resolver=RESOLVER,
        delay=0,
        sleep=sleeps.append,
        retry_budget=4,
    )
    reader = SnuhSourceReader(SnuhAdapter(fetcher), KEY, 200, 1_000_000)
    with pytest.raises(SiteUnavailableError):
        reader.read(lambda sha, name, content: sha)


def test_single_broken_page_is_still_just_one_flagged_post():
    """A page that keeps failing while the budget remains is flagged, as before."""

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.path == "/hrpp/bbs/list.do":
            return httpx.Response(200, text=list_html([row("1", "A")]))
        return httpx.Response(502)

    fetcher = SafeFetcher(
        transport=httpx.MockTransport(handler),
        resolver=RESOLVER,
        delay=0,
        sleep=lambda s: None,
        retry_budget=10,
    )
    reading = SnuhSourceReader(SnuhAdapter(fetcher), KEY, 200, 1_000_000).read(lambda *a: "x")
    assert list(reading.failures) == ["1"] and "gave up after 3 tries" in reading.failures["1"]


def test_scan_service_reports_site_unavailable_as_failed_with_the_reason():
    from datetime import UTC, datetime

    from irb_watch.services.scan import ScanService

    repo = FakeScanRepo()
    repo.add_source(make_source(1))
    reader = FakeReader(error=SiteUnavailableError("site unavailable: HTTP 502; stopped after 10 retries"))
    service = ScanService(
        repo, FakeFileStorage(), lambda s: reader, FakeAudit(), clock=lambda: datetime(2026, 11, 6, tzinfo=UTC)
    )
    result = service.scan_source(1, "admin-1")
    assert result.status == "failed" and "stopped after 10 retries" in result.error
    assert repo.get_source(1).last_scan_at is None


# ---- login: the form load retries, the submit never does ----------------------------------

GOOD = Credentials("tester", "s3cret-pw")
FORM = """<html><body><form id="foLgnForm" method="post" action="/hrpp/lgn/login.do">
<input type="text" name="userId" id="login-userId">
<input type="password" name="userPswd" id="login-userPswd">
<button type="submit">login</button></form></body></html>"""


class Site(BaseHTTPRequestHandler):
    form_failures = 0  # how many form loads still answer form_code
    form_code = 502
    form_loads = 0  # how many times the form was requested
    post_status = 302  # what the submit answers
    posts = 0

    def log_message(self, *args):
        pass

    def _send(self, code, body="", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path.startswith("/hrpp/lgn/form.do"):
            Site.form_loads += 1
            if Site.form_failures > 0:
                Site.form_failures -= 1
                return self._send(Site.form_code, "error page")
            return self._send(200, FORM)
        if self.path.startswith("/hrpp/bbs/list.do"):
            return self._send(200, "<html>board</html>")
        self._send(404)

    def do_POST(self):
        Site.posts += 1
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if Site.post_status == 302:
            self._send(302, headers={"Location": "/hrpp/bbs/list.do", "Set-Cookie": "SID=ok; Path=/"})
        else:
            self._send(Site.post_status, "bad gateway")


@pytest.fixture
def site():
    server = HTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Site.form_failures, Site.form_code, Site.form_loads = 0, 502, 0
    Site.post_status, Site.posts = 302, 0
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def login(base, **kw):
    return browser_login(
        snuh_login_url(base, "/hrpp/bbs/list.do"),
        GOOD,
        timeout_ms=3000,
        url_check=lambda url: None,
        sleep=kw.pop("sleep", lambda s: None),
        **kw,
    )


def test_login_form_502_is_retried_then_login_succeeds(site):
    Site.form_failures = 2
    sleeps = []
    assert login(site, sleep=sleeps.append)["cookies"][0]["name"] == "SID"
    assert sleeps == [5.0, 15.0]


def test_login_form_always_502_says_site_unavailable_not_wrong_password(site):
    Site.form_failures = 99
    with pytest.raises(SiteUnavailableError, match=r"HTTP 502.*gave up after 3 tries") as info:
        login(site)
    assert not isinstance(info.value, LoginRejectedError)
    assert Site.posts == 0  # the password was never submitted


@pytest.mark.parametrize("code", [403, 404, 500])
def test_a_non_temporary_error_on_the_login_form_fails_at_once_and_is_not_retryable(site, code):
    Site.form_failures, Site.form_code = 99, code
    with pytest.raises(FetchError, match=f"HTTP {code}") as info:
        login(site)
    assert not isinstance(info.value, SiteUnavailableError)  # so no retry is planned later
    assert Site.form_loads == 1 and Site.posts == 0


def test_502_after_submit_is_reported_as_site_error_and_not_retried(site):
    Site.post_status = 502
    with pytest.raises(SiteUnavailableError, match="after submitting.*not retried"):
        login(site)
    assert Site.posts == 1
