class CrawlError(Exception):
    """Base for failures that must mark a source `needs_review`, never "no change"."""


class FetchError(CrawlError):
    """Network, HTTP status, redirect, size or URL-safety failure."""


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
