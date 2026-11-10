"""Where a source's stored login lives, and the fresh login for SNUH sources that need one."""

from collections.abc import Callable

import httpx

from irb_watch.crawlers.credentials import CredentialStore
from irb_watch.crawlers.login import fresh_login, snuh_login_url
from irb_watch.crawlers.rbay_login import rbay_site_name
from irb_watch.crawlers.sessions import MemorySessionStore
from irb_watch.crawlers.snuh import BASE_URL
from irb_watch.services.sources import RBAY_ADAPTER, SNUH_LIST_PATH, Source


def login_site(source: Source) -> str | None:
    """The one place that names where a source's stored login is filed (the admin API shows it,
    the scans read it). None for a source that scans without a login.
      * SNUH board:  snuh-<board key>
      * R-BAY board: rbay-<hospital code> (one login per hospital, shared by its boards)
    """
    if not source.config.get("login_user"):
        return None
    if source.adapter == RBAY_ADAPTER:
        return rbay_site_name(source.config["agency"])
    return f"snuh-{source.config['board_key']}"


def cookies_for_source(
    source: Source,
    credential_store: CredentialStore,
    login: Callable[..., httpx.Cookies] = fresh_login,
) -> httpx.Cookies | None:
    """None for a public SNUH source. Otherwise log in now (one attempt) and return the cookies.

    Raises MissingLoginError when no login is stored (flagged, not paused) and
    LoginRejectedError (with a reason) when the site refuses it. Sessions idle out after 30
    minutes, so every scan logs in fresh, keeps the session in memory only, and never retries a
    rejected login.
    """
    site = login_site(source)
    if site is None:
        return None
    return_path = f"{SNUH_LIST_PATH}?key={source.config['board_key']}"
    return login(
        credential_store,
        MemorySessionStore(),
        source.config["login_user"],
        site,
        snuh_login_url(BASE_URL, return_path),
    )
