"""The R-BAY login form: a refusal is an in-page notice, not a browser dialog (seen live 2026-11-20).

Runs a fake copy of the login page inside headless Chromium (R-BAY is never contacted)."""

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from irb_watch.crawlers.credentials import Credentials
from irb_watch.crawlers.errors import LayoutChangedError, LoginRejectedError, SiteUnavailableError
from irb_watch.crawlers.rbay_login import _submit_login

LOGIN = """<html><body><input id="loginId"><input id="loginPwd"><button id="btnLogin">login</button>
<div id="usrLoginMsgPopup" style="display:none"><p id="usrLoginRsltText"></p></div>
<script>
btnLogin.onclick = () => {
  if (loginPwd.value === 'good') { location.href = '/main'; }
  else if (loginPwd.value === 'silent') { /* the site says nothing */ }
  else if (loginPwd.value === 'notice-then-good') { alert('notice'); setTimeout(() => { location.href = '/main'; }, 600); }
  else if (loginPwd.value === 'dialog-only') { alert('computer says no'); }
  else { usrLoginMsgPopup.style.display = 'block';
         usrLoginRsltText.textContent = '아이디 비밀번호가 일치 하지 않습니다. 로그인에 5회 실패하면 로그인이 제한됩니다.'; }
};
</script></body></html>"""
NO_FORM = "<html><body>maintenance</body></html>"


@pytest.fixture
def page():
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except PlaywrightError:
            pytest.skip("Chromium is not installed (uv run playwright install chromium)")
        context = browser.new_context()
        state = {"login": LOGIN}

        def serve(route):
            url = route.request.url
            body = state["login"] if url.endswith("/login") else "<html><body>main</body></html>"
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

        context.route("https://www.r-bay.co.kr/**", serve)
        p = context.new_page()
        p.set_default_timeout(5000)
        yield p, state
        browser.close()


def test_a_refusal_notice_is_a_rejected_login_with_the_sites_reason(page):
    p, _ = page
    with pytest.raises(LoginRejectedError) as exc:
        _submit_login(p, Credentials("someone", "bad"), [], 4000)
    assert "일치 하지 않습니다" in str(exc.value) and "bad" not in str(exc.value)


def test_a_good_login_leaves_the_login_page(page):
    p, _ = page
    _submit_login(p, Credentials("someone", "good"), [], 4000)
    assert "/login" not in p.url


def test_a_dialog_before_a_good_redirect_is_not_a_rejection(page):
    p, _ = page
    dialogs: list[str] = []
    p.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    _submit_login(p, Credentials("someone", "notice-then-good"), dialogs, 6000)
    assert "/login" not in p.url and dialogs == ["notice"]


def test_a_dialog_with_no_redirect_and_no_notice_is_the_reason_after_the_wait(page):
    p, _ = page
    dialogs: list[str] = []
    p.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    with pytest.raises(LoginRejectedError) as exc:
        _submit_login(p, Credentials("someone", "dialog-only"), dialogs, 1500)
    assert "computer says no" in str(exc.value)


def test_silence_is_site_trouble_not_a_rejection(page):
    p, _ = page
    with pytest.raises(SiteUnavailableError):
        _submit_login(p, Credentials("someone", "silent"), [], 1500)


def test_a_changed_form_is_reported_as_such(page):
    p, state = page
    state["login"] = NO_FORM
    with pytest.raises(LayoutChangedError):
        _submit_login(p, Credentials("someone", "good"), [], 1500)
