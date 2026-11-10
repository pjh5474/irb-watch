"""Automatic login for members-only boards with a headless browser.

Fills the site's own login form (so its JavaScript runs as it does for a person), then
returns Playwright's storage_state. The password is used only inside this call and never
appears in errors. Sync Playwright cannot run on an asyncio event loop thread: call this
from a worker thread.
"""

import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from irb_watch.crawlers.credentials import Credentials, CredentialStore
from irb_watch.crawlers.errors import (
    FetchError,
    LoginRejectedError,
    MissingLoginError,
    SiteUnavailableError,
)
from irb_watch.crawlers.fetcher import DEFAULT_RETRY_WAITS, RETRYABLE_STATUS
from irb_watch.crawlers.sessions import SessionStore, cookies_from_state
from irb_watch.security.url_guard import validate_url

SNUH_ID_FIELD = "#login-userId"
SNUH_PASSWORD_FIELD = "#login-userPswd"
SNUH_FORM = "#foLgnForm"
_LOGIN_PATH = re.compile(r"login|logon|/lgn/", re.IGNORECASE)


def snuh_login_url(base_url: str, return_path: str) -> str:
    return f"{base_url}/hrpp/lgn/form.do?{urlencode({'returnUrl': return_path})}"


def _load_login_form(
    page, login_url: str, retry_waits: tuple[float, ...], sleep: Callable[[float], None]
) -> None:
    """Open the login form; retry temporary site trouble (loading the form is safe to repeat)."""
    tries = len(retry_waits) + 1
    for attempt in range(1, tries + 1):
        try:
            response = page.goto(login_url)
        except PlaywrightError as exc:  # includes timeouts and connection failures
            first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            problem = f"could not open the login page: {first_line}"
        else:
            status = response.status if response is not None else 200
            if status < 400:
                return
            if status not in RETRYABLE_STATUS:
                # Not temporary (403, 404, 500 ...): fail now, and the scheduler will not
                # plan a retry for it.
                raise FetchError(f"login page answered HTTP {status}")
            problem = f"login page answered HTTP {status}"
        if attempt == tries:
            raise SiteUnavailableError(f"{problem} (gave up after {tries} tries)")
        sleep(retry_waits[attempt - 1])


def browser_login(
    login_url: str,
    credentials: Credentials,
    *,
    id_field: str = SNUH_ID_FIELD,
    password_field: str = SNUH_PASSWORD_FIELD,
    form: str = SNUH_FORM,
    timeout_ms: int = 20_000,
    url_check: Callable[[str], object] = validate_url,
    retry_waits: tuple[float, ...] = DEFAULT_RETRY_WAITS,
    sleep: Callable[[float], None] = time.sleep,
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
            # Status of the last form submission (a POST navigation), to tell "the site
            # errored" from "the site said no".
            submit_status: list[int] = []
            page.on(
                "response",
                lambda r: submit_status.append(r.status)
                if r.request.method == "POST" and r.request.is_navigation_request()
                else None,
            )
            _load_login_form(page, login_url, retry_waits, sleep)
            try:
                page.fill(id_field, credentials.login_id)
                page.fill(password_field, credentials.password)
                page.locator(f"{form} button[type=submit]").click()
                page.wait_for_url(lambda url: not _LOGIN_PATH.search(urlsplit(url).path))
                page.wait_for_load_state("load")
            except PlaywrightTimeout:
                if submit_status and submit_status[-1] >= 500:
                    # Never retried: repeating a submission could count as a second
                    # failed attempt on the account.
                    raise SiteUnavailableError(
                        f"site error after submitting the login (HTTP {submit_status[-1]}); "
                        "not retried, so the credentials were not judged"
                    ) from None
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
        # A setup gap, not a refusal: nothing was sent, so the source is flagged, not paused.
        raise MissingLoginError(
            f"no stored credentials for user '{user}' on site '{site}'; "
            "save the login on the source's admin page"
        )
    state = login(login_url, credentials)
    session_store.save(user, site, state)
    return cookies_from_state(state)
