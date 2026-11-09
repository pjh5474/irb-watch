"""Fresh login for sources that need one (`config.login_user` is set)."""

from collections.abc import Callable

import httpx

from irb_watch.crawlers.credentials import CredentialStore
from irb_watch.crawlers.login import fresh_login, snuh_login_url
from irb_watch.crawlers.sessions import MemorySessionStore
from irb_watch.crawlers.snuh import BASE_URL
from irb_watch.services.sources import SNUH_LIST_PATH, Source


def credential_site(source: Source) -> str:
    """Name the stored credentials are filed under: snuh-<board_key>."""
    return f"snuh-{source.config['board_key']}"


def cookies_for_source(
    source: Source,
    credential_store: CredentialStore,
    login: Callable[..., httpx.Cookies] = fresh_login,
) -> httpx.Cookies | None:
    """None for a public source. Otherwise log in now (one attempt) and return the cookies.

    Raises LoginRejectedError (with a reason) when there are no stored credentials or the
    site refuses the login. Sessions idle out after 30 minutes, so every scan logs in fresh,
    keeps the session in memory only, and never retries a rejected login.
    """
    user = source.config.get("login_user")
    if not user:
        return None
    return_path = f"{SNUH_LIST_PATH}?key={source.config['board_key']}"
    return login(
        credential_store,
        MemorySessionStore(),
        user,
        credential_site(source),
        snuh_login_url(BASE_URL, return_path),
    )
