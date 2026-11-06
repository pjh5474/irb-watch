"""Automatic login for members-only boards with a headless browser.

Fills the site's own login form (so its JavaScript runs as it does for a person), then
returns Playwright's storage_state. The password is used only inside this call and never
appears in errors. Sync Playwright cannot run on an asyncio event loop thread: call this
from a worker thread.
"""

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from irb_watch.crawlers.credentials import Credentials, CredentialStore
from irb_watch.crawlers.errors import LoginRejectedError
from irb_watch.crawlers.sessions import SessionStore, cookies_from_state
from irb_watch.security.url_guard import validate_url

SNUH_ID_FIELD = "#login-userId"
SNUH_PASSWORD_FIELD = "#login-userPswd"
SNUH_FORM = "#foLgnForm"
_LOGIN_PATH = re.compile(r"login|logon|/lgn/", re.IGNORECASE)


def snuh_login_url(base_url: str, return_path: str) -> str:
    return f"{base_url}/hrpp/lgn/form.do?{urlencode({'returnUrl': return_path})}"


def browser_login(
    login_url: str,
    credentials: Credentials,
    *,
    id_field: str = SNUH_ID_FIELD,
    password_field: str = SNUH_PASSWORD_FIELD,
    form: str = SNUH_FORM,
    timeout_ms: int = 20_000,
    url_check: Callable[[str], object] = validate_url,
) -> dict[str, Any]:
    """Log in and return storage_state. Raises LoginRejectedError with a reason on failure."""
    try:
        url_check(login_url)
    except ValueError as exc:
        raise LoginRejectedError(f"login URL not allowed: {exc}") from exc
    host = urlsplit(login_url).netloc

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            try:
                page.goto(login_url)
                page.fill(id_field, credentials.login_id)
                page.fill(password_field, credentials.password)
                page.locator(f"{form} button[type=submit]").click()
                page.wait_for_url(lambda url: not _LOGIN_PATH.search(urlsplit(url).path))
                page.wait_for_load_state("load")
            except PlaywrightTimeout:
                raise LoginRejectedError(
                    "login rejected: still on the login page after submitting "
                    "(wrong ID/password, blocked account, or the form changed)"
                ) from None
            except PlaywrightError as exc:
                first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                raise LoginRejectedError(f"login failed: browser error: {first_line}") from None

            if urlsplit(page.url).netloc != host:
                raise LoginRejectedError("login redirected to a different site; refusing to save it")
            state = context.storage_state()
        finally:
            browser.close()

    if not state.get("cookies"):
        raise LoginRejectedError("login produced no cookies")
    return state


def fresh_login(
    credential_store: CredentialStore,
    session_store: SessionStore,
    user: str,
    site: str,
    login_url: str,
    login: Callable[..., dict[str, Any]] = browser_login,
) -> httpx.Cookies:
    """Log in now with the stored credentials, save the session, return its cookies.

    Call at the start of each scan: sessions idle out after 30 minutes, so a saved one
    from the previous scan is assumed dead.
    """
    credentials = credential_store.get(user, site)
    if credentials is None:
        raise LoginRejectedError(f"no stored credentials for user '{user}' on site '{site}'")
    state = login(login_url, credentials)
    session_store.save(user, site, state)
    return cookies_from_state(state)
