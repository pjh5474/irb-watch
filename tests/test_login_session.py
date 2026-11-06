import httpx
import pytest

from irb_watch.core.models import AttachmentRef
from irb_watch.crawlers.errors import LoginExpiredError
from irb_watch.crawlers.fetcher import SafeFetcher
from irb_watch.crawlers.sessions import FileSessionStore, cookies_from_state, load_cookies
from irb_watch.crawlers.snuh import SnuhAdapter, is_login_page
from tests.test_snuh_adapter import KEY, RESOLVER, _summary, detail_html, list_html, row

STATE = {
    "cookies": [
        {"name": "JSESSIONID", "value": "abc", "domain": "hrpp.snuh.org", "path": "/"},
    ],
    "origins": [],
}
LOGIN_FORM = '<form action="/hrpp/login.do"><input type="password" name="pw"></form>'


def adapter(handler, cookies=None):
    fetcher = SafeFetcher(
        transport=httpx.MockTransport(handler),
        resolver=RESOLVER,
        delay=0,
        sleep=lambda s: None,
        cookies=cookies,
    )
    return SnuhAdapter(fetcher)


def robots_ok(request):
    return httpx.Response(200, text="User-agent: *\nAllow: /") if request.url.path == "/robots.txt" else None


# ---- session store --------------------------------------------------------


def test_file_store_roundtrip_and_missing(tmp_path):
    store = FileSessionStore(tmp_path)
    assert store.load("owner", "snuh-31") is None
    store.save("owner", "snuh-31", STATE)
    assert store.load("owner", "snuh-31") == STATE
    assert store.load("someone-else", "snuh-31") is None


@pytest.mark.parametrize("bad", ["..", "../x", "a/b", "a\\b", "", ".hidden"])
def test_file_store_rejects_path_tricks(tmp_path, bad):
    store = FileSessionStore(tmp_path)
    with pytest.raises(ValueError):
        store.save(bad, "site", STATE)
    with pytest.raises(ValueError):
        store.load("owner", bad)


def test_missing_or_empty_session_is_login_error_with_reason(tmp_path):
    store = FileSessionStore(tmp_path)
    with pytest.raises(LoginExpiredError, match="no saved login"):
        load_cookies(store, "owner", "snuh-31")
    store.save("owner", "snuh-31", {"cookies": [], "origins": []})
    with pytest.raises(LoginExpiredError, match="no saved login"):
        load_cookies(store, "owner", "snuh-31")


def test_cookies_are_sent_to_their_host_only():
    seen = {}

    def handler(request):
        if (r := robots_ok(request)) is not None:
            return r
        seen[request.url.host] = request.headers.get("cookie")
        return httpx.Response(200, text="ok")

    fetcher = SafeFetcher(
        transport=httpx.MockTransport(handler),
        resolver=RESOLVER,
        delay=0,
        sleep=lambda s: None,
        check_robots=False,
        cookies=cookies_from_state(STATE),
    )
    fetcher.get("https://hrpp.snuh.org/x")
    fetcher.get("https://other.example.com/x")
    assert seen["hrpp.snuh.org"] == "JSESSIONID=abc"
    assert seen["other.example.com"] is None


# ---- login failures are their own error -----------------------------------


def test_redirect_to_login_url_is_login_error_not_empty_board():
    def handler(request):
        if (r := robots_ok(request)) is not None:
            return r
        if request.url.path == "/hrpp/bbs/list.do":
            return httpx.Response(302, headers={"location": "/hrpp/login.do"})
        return httpx.Response(200, text=LOGIN_FORM)

    with pytest.raises(LoginExpiredError, match="login"):
        adapter(handler).list_board(KEY)


def test_login_form_served_on_board_url_is_login_error():
    def handler(request):
        if (r := robots_ok(request)) is not None:
            return r
        return httpx.Response(200, text=f"<html>{LOGIN_FORM}</html>")

    with pytest.raises(LoginExpiredError):
        adapter(handler).list_board(KEY)


def test_login_form_on_detail_page_is_login_error():
    def handler(request):
        if (r := robots_ok(request)) is not None:
            return r
        return httpx.Response(200, text=f"<html>{LOGIN_FORM}</html>")

    with pytest.raises(LoginExpiredError):
        adapter(handler).fetch_post(_summary())


def test_login_page_instead_of_download_is_login_error():
    def handler(request):
        if (r := robots_ok(request)) is not None:
            return r
        return httpx.Response(200, text=LOGIN_FORM, headers={"content-type": "text/html"})

    ref = AttachmentRef("a.pdf", "1 KB", "https://hrpp.snuh.org/atch/file/down.do?enc=A")
    with pytest.raises(LoginExpiredError):
        adapter(handler).download(ref)


def test_normal_board_with_password_field_for_secret_posts_still_parses():
    """A board page that has its normal markup is not a login page, even with a password box."""
    html = list_html([row("1", "A")]) + '<input type="password" id="secretPw">'

    def handler(request):
        if (r := robots_ok(request)) is not None:
            return r
        if request.url.path == "/hrpp/bbs/list.do":
            return httpx.Response(200, text=html)
        return httpx.Response(200, text=detail_html())

    total, posts = adapter(handler).list_board(KEY)
    assert total == 1 and posts[0].post_id == "1"


def test_cookies_reach_the_board_through_the_adapter():
    def handler(request):
        if (r := robots_ok(request)) is not None:
            return r
        if request.url.path == "/hrpp/login.do":
            return httpx.Response(200, text=LOGIN_FORM)
        if request.headers.get("cookie") != "JSESSIONID=abc":
            return httpx.Response(302, headers={"location": "/hrpp/login.do"})
        return httpx.Response(200, text=list_html([row("1", "A")]))

    total, _ = adapter(handler, cookies_from_state(STATE)).list_board(KEY)
    assert total == 1
    with pytest.raises(LoginExpiredError):
        adapter(handler).list_board(KEY)


def test_is_login_page_url_and_form():
    assert is_login_page("https://hrpp.snuh.org/hrpp/login.do", "<html></html>")
    assert is_login_page("https://hrpp.snuh.org/x", LOGIN_FORM)
    assert not is_login_page("https://hrpp.snuh.org/hrpp/bbs/list.do", "<html></html>")
