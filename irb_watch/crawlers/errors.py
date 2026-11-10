class CrawlError(Exception):
    """Base for failures that must mark a source `needs_review`, never "no change"."""


class FetchError(CrawlError):
    """Network, HTTP status, redirect, size or URL-safety failure."""


class TransientFetchError(FetchError):
    """One request kept failing temporarily (502/503/504, timeouts) through all its tries.

    Per post this is flagged like any broken post. When it hits the list page the scan fails
    with this reason, and the scheduler may try that source again later.
    """


class SiteUnavailableError(FetchError):
    """The site kept failing (502/503/504, timeouts, no connection) and retrying did not help.

    Also raised when a scan used up its whole retry budget. A scan that gets this stops
    with status `failed` and this reason; nothing is changed.
    """


class RobotsDisallowedError(CrawlError):
    """robots.txt forbids fetching this URL."""


class LayoutChangedError(CrawlError):
    """The page no longer matches what the adapter expects."""


class IncompleteScanError(CrawlError):
    """Fewer posts were read than the site declared."""


class CredentialStoreError(CrawlError):
    """The stored hospital login cannot be read (missing or wrong CREDENTIALS_KEY, damaged
    data, or the credential store is unreachable). No hospital login was tried.

    The source becomes `needs_review` with this reason; it is not paused, because nothing
    was sent to the hospital site.
    """


class MissingLoginError(CredentialStoreError):
    """No login is stored for this source yet (a setup gap, not a refusal). Nothing was sent to
    the site, so the source becomes needs_review with this reason and is NOT paused: once the
    login is saved, the next scheduled scan simply works."""


class ScanTooLargeError(CrawlError):
    """The scan's data is too big to save in one transaction; nothing was saved."""


class LoginRejectedError(CrawlError):
    """Automatic login failed: wrong ID/password, blocked account, or the login form changed.

    Affects only the one source being scanned: it becomes `needs_review` with this reason.
    """


class LoginExpiredError(CrawlError):
    """The board needs a login and the saved session is missing, expired or rejected.

    Affects only the one source being scanned: it becomes `needs_review` with this reason.
    """
