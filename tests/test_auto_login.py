"""Auto-login against a local fake login site (real headless Chromium, no internet)."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest

from irb_watch.crawlers.credentials import Credentials
from irb_watch.crawlers.errors import LoginRejectedError
from irb_watch.crawlers.login import browser_login, fresh_login, snuh_login_url
from irb_watch.crawlers.sessions import FileSessionStore
from tests.fakes_scan import MemCreds

GOOD = Credentials("tester", "s3cret-pw")
FORM = """<html><body><form id="foLgnForm" method="post" action="/hrpp/lgn/login.do">
<input type="hidden" name="returnUrl" value="/hrpp/bbs/list.do">
<input type="text" name="userId" id="login-userId">
<input type="password" name="userPswd" id="login-userPswd">
<button type="submit">login</button></form></body></html>"""


class FakeSite(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
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
            self._send(200, FORM)
        elif self.path.startswith("/hrpp/bbs/list.do"):
            self._send(200, "<html>board</html>")
        else:
            self._send(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode())
        ok = form.get("userId") == [GOOD.login_id] and form.get("userPswd") == [GOOD.password]
        if ok:
            self._send(302, headers={"Location": "/hrpp/bbs/list.do", "Set-Cookie": "SID=ok; Path=/"})
        else:
            self._send(200, FORM)  # same login page again, no redirect


@pytest.fixture(scope="module")
def base_url():
    server = HTTPServer(("127.0.0.1", 0), FakeSite)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def login(base_url, creds, timeout_ms=3000):
    return browser_login(
        snuh_login_url(base_url, "/hrpp/bbs/list.do"),
        creds,
        timeout_ms=timeout_ms,
        url_check=lambda url: None,
    )


def test_login_success_returns_session_cookie(base_url):
    state = login(base_url, GOOD)
    assert [c["name"] for c in state["cookies"]] == ["SID"]


def test_wrong_password_is_rejected_with_reason_and_no_password_in_message(base_url):
    with pytest.raises(LoginRejectedError, match="still on the login page") as info:
        login(base_url, Credentials("tester", "wrong-pw-123"), timeout_ms=1500)
    assert "wrong-pw-123" not in str(info.value)


def test_unsafe_login_url_is_refused_before_launching_a_browser():
    with pytest.raises(LoginRejectedError, match="not allowed"):
        browser_login("http://127.0.0.1/hrpp/lgn/form.do", GOOD)


def test_credentials_repr_hides_password():
    assert "s3cret-pw" not in repr(GOOD)


# ---- fresh_login orchestration (fake browser) -------------------------------


def test_fresh_login_saves_session_and_returns_cookies(tmp_path):
    state = {"cookies": [{"name": "SID", "value": "v", "domain": "hrpp.snuh.org", "path": "/"}]}
    sessions = FileSessionStore(tmp_path)
    cookies = fresh_login(
        MemCreds(GOOD), sessions, "owner", "s1", "https://hrpp.snuh.org/x", login=lambda u, c: state
    )
    assert cookies.get("SID", domain="hrpp.snuh.org") == "v"
    assert sessions.load("owner", "s1") == state


def test_fresh_login_without_stored_credentials_is_rejected_with_reason(tmp_path):
    with pytest.raises(LoginRejectedError, match="no stored credentials"):
        fresh_login(MemCreds(None), FileSessionStore(tmp_path), "owner", "s1", "https://x/y")


def test_fresh_login_failure_saves_nothing(tmp_path):
    def boom(url, creds):
        raise LoginRejectedError("login rejected: test")

    sessions = FileSessionStore(tmp_path)
    with pytest.raises(LoginRejectedError):
        fresh_login(MemCreds(GOOD), sessions, "owner", "s1", "https://x/y", login=boom)
    assert sessions.load("owner", "s1") is None
