"""R-BAY login: a headless browser logs in and presses 입장하기 for one hospital; the scan then
continues over plain HTTP with the session cookie (crawlers/rbay.py).

Why a browser: the login page encrypts the password with RSA in its own JavaScript, and
입장하기 runs an AJAX check before opening the e-IRB. Verified live on 2026-11-10: no CAPTCHA,
no extra verification, no pop-ups for this account.

One login per hospital per run (owner: "too many logins look not good"). The boards of one
hospital are scanned one after another by the same site worker, so the pool hands the same
session to each of them, and logs it out IDLE_LOGOUT seconds after the last board is done.

What counts as what (a rejection pauses the source; site trouble is retried an hour later):
  * the site said no to the password (a pop-up on the login page)  -> LoginRejectedError,
    remembered for the whole account, so the next board does not send that password again;
  * the account cannot enter this hospital (no 입장하기, a pop-up, or another page instead of
    the e-IRB) -> EntryRefusedError, remembered for that hospital only;
  * no answer, a timeout or a browser error -> SiteUnavailableError, nothing remembered;
  * the login form itself changed -> LayoutChangedError.
A scan a person starts may try a remembered login again (that is how a pause is lifted).
"""

import hashlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from irb_watch.crawlers.credentials import Credentials, CredentialStore
from irb_watch.crawlers.errors import (
    LayoutChangedError,
    LoginRejectedError,
    MissingLoginError,
    SiteUnavailableError,
)
from irb_watch.crawlers.fetcher import SafeFetcher
from irb_watch.crawlers.rbay import (
    BASE_URL,
    HOST,
    BoardMenu,
    agency_url,
    logout_form,
    parse_gate_menus,
)
from irb_watch.crawlers.sessions import valid_name
from irb_watch.crawlers.tls import rbay_ssl_context
from irb_watch.security.url_guard import validate_url

log = logging.getLogger(__name__)

SESSION_REUSE = 15 * 60.0  # seconds a session is handed to the next board of the same hospital
IDLE_LOGOUT = 90.0  # seconds after the last board before logging out
REJECTED_FOR = 60 * 60.0
_ACCOUNT = "*"  # rejection scope: the password, for every hospital


# The single site name used before logins were per hospital (2026-11-10). Read only as a
# fallback, so a login saved under it keeps working until it is saved per hospital.
LEGACY_SITE = "rbay"


def rbay_site_name(agency: str) -> str:
    """The naming rule for a hospital's R-BAY login: rbay-<hospital code>. Per hospital, because
    an R-BAY account can enter only the hospitals it is registered with (owner, 2026-11-10);
    the boards of one hospital share it. A code the store cannot use as a name (base64 '='
    padding, too long) becomes a stable hash. Callers use services.source_login.login_site,
    which the admin page shows, so nobody has to work this out by hand."""
    name = f"rbay-{agency}"
    if valid_name(name):
        return name
    return f"rbay-{hashlib.sha256(agency.encode()).hexdigest()[:24]}"


def stored_login(store: CredentialStore, user: str, site: str) -> tuple[Credentials | None, str]:
    """(the login, the site it was found under): the hospital's own site, else the legacy one."""
    found = store.get(user, site)
    if found is not None or site == LEGACY_SITE:
        return found, site
    legacy = store.get(user, LEGACY_SITE)
    if legacy is not None:
        log.warning("R-BAY login for %s read from the old shared site %r; save it as %r", user, LEGACY_SITE, site)
        return legacy, LEGACY_SITE
    return None, site


class EntryRefusedError(LoginRejectedError):
    """The login worked but this account cannot enter this hospital's e-IRB."""


@dataclass
class RbaySession:
    user: str
    agency: str
    cookies: list[dict]  # Playwright cookie dicts, R-BAY's host only
    user_agent: str
    gate_html: str
    menus: dict[str, BoardMenu]
    created: float = field(default_factory=time.monotonic)
    closed: bool = False
    in_use: int = 0  # boards reading with it right now; never logged out while > 0

    def jar(self) -> httpx.Cookies:
        jar = httpx.Cookies()
        for c in self.cookies:
            jar.set(c["name"], c["value"], domain=c.get("domain", HOST), path=c.get("path", "/"))
        return jar

    def headers(self) -> dict[str, str]:
        # The site checks nothing else; a browser's own User-Agent and the e-IRB as referrer
        # make the requests look like the page's own.
        return {"User-Agent": self.user_agent, "Referer": f"{BASE_URL}/mng/gate", "Origin": BASE_URL}


def _first_line(exc: Exception) -> str:
    return (str(exc).splitlines() or [type(exc).__name__])[0][:200]


def browser_enter(agency: str, credentials: Credentials, timeout_ms: int = 20_000) -> RbaySession:
    """Log in and enter one hospital's e-IRB. See the module notes for which error means what."""
    login_url, start_url = f"{BASE_URL}/login", agency_url(agency)
    for url in (login_url, start_url):
        validate_url(url)
    dialogs: list[str] = []

    def on_dialog(dialog) -> None:
        dialogs.append(dialog.message.strip()[:200])
        dialog.dismiss()  # never accept: an "OK" could confirm something

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            page.on("dialog", on_dialog)
            page.goto(login_url, wait_until="domcontentloaded")
            if page.locator("#loginId").count() == 0 or page.locator("#loginPwd").count() == 0:
                raise LayoutChangedError("the R-BAY login form changed (ID or password box not found)")
            page.fill("#loginId", credentials.login_id)
            page.fill("#loginPwd", credentials.password)
            page.click("#btnLogin")
            try:
                page.wait_for_url(lambda u: "/login" not in u, timeout=timeout_ms)
            except PlaywrightTimeout:
                if dialogs:  # the site told us why: that is a real answer about the password
                    raise LoginRejectedError(f"R-BAY login rejected: {dialogs[-1]}") from None
                raise SiteUnavailableError(
                    f"R-BAY did not finish the login within {timeout_ms // 1000} s (no answer given)"
                ) from None

            page.goto(start_url, wait_until="domcontentloaded")
            enter = page.locator("a:has-text('입장하기')")
            if enter.count() == 0:
                raise EntryRefusedError(
                    "this R-BAY account has no 입장하기 for this hospital (not registered with its e-IRB)"
                )
            enter.first.click()
            try:
                page.wait_for_url("**/mng/gate**", timeout=timeout_ms)
                page.wait_for_load_state("domcontentloaded")
            except PlaywrightTimeout:
                if dialogs:
                    raise EntryRefusedError(f"could not enter the hospital's e-IRB: {dialogs[-1]}") from None
                where = urlsplit(page.url).path
                if where != urlsplit(start_url).path:
                    raise EntryRefusedError(
                        f"could not enter the hospital's e-IRB: R-BAY opened {where} instead "
                        "(an extra verification step may be required)"
                    ) from None
                raise SiteUnavailableError(
                    f"R-BAY did not open the e-IRB within {timeout_ms // 1000} s"
                ) from None
            gate_html = page.content()
            cookies = [c for c in context.cookies() if c.get("domain", "").lstrip(".") in (HOST, "r-bay.co.kr")]
            user_agent = page.evaluate("navigator.userAgent")
        except PlaywrightError as exc:  # includes timeouts of goto/click: site trouble
            raise SiteUnavailableError(f"R-BAY could not be reached by the browser: {_first_line(exc)}") from None
        finally:
            browser.close()
    if not cookies:
        raise SiteUnavailableError("R-BAY gave no session cookie after the login")
    return RbaySession("", agency, cookies, user_agent, gate_html, parse_gate_menus(gate_html))


def logout(session: RbaySession, fetcher_factory: Callable[[RbaySession], SafeFetcher] | None = None) -> bool:
    """Send the e-IRB's own logout (POST /workLogout with its header form). True when the site
    accepted it. A failed logout is logged: the session then ends by R-BAY's idle timeout."""
    make = fetcher_factory or _fetcher_for
    fetcher = make(session)
    try:
        response = fetcher.post(
            f"{BASE_URL}/workLogout", data=logout_form(session.gate_html), follow_redirects=False
        )
        location = response.headers.get("location", "")
        ok = response.status < 400 and "403" not in location and "error" not in location
        if not ok:
            log.warning("R-BAY logout not accepted (HTTP %s, to %s)", response.status, location)
        return ok
    except Exception:
        log.exception("R-BAY logout failed; the session will end by idle timeout")
        return False
    finally:
        fetcher.close()


def _fetcher_for(session: RbaySession) -> SafeFetcher:
    return SafeFetcher(cookies=session.jar(), headers=session.headers(), verify=rbay_ssl_context())


@dataclass
class Lease:
    session: RbaySession
    fresh: bool  # True when this very call logged in (a reused session may have expired)


class RbaySessionPool:
    """Shares one session per (account, hospital) between the boards of one run.

    Locking: one lock per (account, hospital) is held for the whole acquire, so two boards of
    the same hospital never log in twice at once; the shared lock only guards the dictionaries
    and is never held during a login or logout, so other hospitals are not held up.
    """

    def __init__(
        self,
        login: Callable[[str, Credentials], RbaySession] = browser_enter,
        do_logout: Callable[[RbaySession], bool] = logout,
        clock: Callable[[], float] = time.monotonic,
        timer: Callable[[float, Callable[[], None]], threading.Timer] | None = None,
    ) -> None:
        self._login = login
        self._logout = do_logout
        self._clock = clock
        self._timer = timer or _daemon_timer
        self._lock = threading.Lock()
        self._key_locks: dict[tuple[str, str], threading.Lock] = {}
        self._sessions: dict[tuple[str, str], RbaySession] = {}
        self._pending: dict[tuple[str, str], tuple[object, threading.Timer]] = {}
        self._rejected: dict[tuple[str, str], tuple[float, str]] = {}

    def _key_lock(self, key: tuple[str, str]) -> threading.Lock:
        with self._lock:
            return self._key_locks.setdefault(key, threading.Lock())

    def acquire(
        self,
        user: str,
        agency: str,
        site: str,
        credentials: CredentialStore,
        person_started: bool = False,
    ) -> Lease:
        """A session for this hospital, logging in when needed. `site` is where the hospital's
        login is stored (services.source_login.login_site). person_started: a scan a person
        started may send a remembered (rejected) login once more."""
        key = (user, agency)
        with self._key_lock(key):
            stale = None
            with self._lock:
                pending = self._pending.pop(key, None)
                if pending is not None:
                    pending[1].cancel()  # a callback already running sees its ticket is gone
                session = self._sessions.get(key)
                if session and not session.closed and self._clock() - session.created < SESSION_REUSE:
                    session.in_use += 1
                    return Lease(session, fresh=False)
                if session is not None:
                    self._sessions.pop(key, None)
                    if session.in_use == 0:
                        stale = session
            if stale is not None:
                self._end(stale)

            stored, _ = stored_login(credentials, user, site)
            if stored is None:
                # A setup gap: flagged, not paused, so saving the login is enough.
                raise MissingLoginError(
                    f"no R-BAY login is stored for '{user}' at this hospital (site '{site}'); "
                    "save it on the source's admin page"
                )
            # Remembered per stored login (a hash, never the password): after an admin saves a
            # new password it is tried at once.
            fingerprint = hashlib.sha256(f"{user}\0{stored.login_id}\0{stored.password}".encode()).hexdigest()
            if not person_started:
                for scope in (_ACCOUNT, agency):
                    seen = self._rejected.get((fingerprint, scope))
                    if seen and self._clock() - seen[0] < REJECTED_FOR:
                        raise LoginRejectedError(
                            f"{seen[1]} (the same login is not tried again within the hour; "
                            "start a scan by hand to try it now)"
                        )
            try:
                session = self._login(agency, stored)
            except EntryRefusedError as exc:
                self._rejected[(fingerprint, agency)] = (self._clock(), str(exc))
                raise
            except LoginRejectedError as exc:
                self._rejected[(fingerprint, _ACCOUNT)] = (self._clock(), str(exc))
                raise
            self._rejected.pop((fingerprint, _ACCOUNT), None)
            self._rejected.pop((fingerprint, agency), None)
            session.user = user
            session.created = self._clock()
            session.in_use = 1
            with self._lock:
                self._sessions[key] = session
            return Lease(session, fresh=True)

    def release(self, session: RbaySession) -> None:
        """The board is done: log out after IDLE_LOGOUT unless another board takes it first."""
        key = (session.user, session.agency)
        with self._lock:
            session.in_use = max(0, session.in_use - 1)
            if self._sessions.get(key) is not session:
                end_now = not session.closed and session.in_use == 0
            else:
                end_now = False
                if session.in_use == 0:
                    old = self._pending.pop(key, None)
                    if old is not None:
                        old[1].cancel()
                    ticket = object()
                    self._pending[key] = (
                        ticket,
                        self._timer(IDLE_LOGOUT, lambda: self._expire(key, session, ticket)),
                    )
        if end_now:  # replaced while this board still used it: log it out now
            self._end(session)

    def discard(self, session: RbaySession) -> None:
        """The session stopped working (the site sent the login page): never hand it out again."""
        key = (session.user, session.agency)
        with self._lock:
            if self._sessions.get(key) is session:
                self._sessions.pop(key, None)
            session.closed = True

    def _expire(self, key: tuple[str, str], session: RbaySession, ticket: object) -> None:
        with self._lock:
            pending = self._pending.get(key)
            # Taken again (ticket gone) or in use: this logout is cancelled.
            if pending is None or pending[0] is not ticket or session.in_use > 0:
                return
            self._pending.pop(key, None)
            if self._sessions.get(key) is session:
                self._sessions.pop(key, None)
        self._end(session)

    def _end(self, session: RbaySession) -> None:
        with self._lock:
            if session.closed:
                return
            session.closed = True
        self._logout(session)


def _daemon_timer(seconds: float, action: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(seconds, action)
    timer.daemon = True
    timer.start()
    return timer


POOL = RbaySessionPool()
