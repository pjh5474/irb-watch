class CrawlError(Exception):
    """Base for failures that must mark a source `needs_review`, never "no change"."""


class FetchError(CrawlError):
    """Network, HTTP status, redirect, size or URL-safety failure."""


class RobotsDisallowedError(CrawlError):
    """robots.txt forbids fetching this URL."""


class LayoutChangedError(CrawlError):
    """The page no longer matches what the adapter expects."""


class IncompleteScanError(CrawlError):
    """Fewer posts were read than the site declared."""


class LoginRejectedError(CrawlError):
    """Automatic login failed: wrong ID/password, blocked account, or the login form changed.

    Affects only the one source being scanned: it becomes `needs_review` with this reason.
    """


class LoginExpiredError(CrawlError):
    """The board needs a login and the saved session is missing, expired or rejected.

    Affects only the one source being scanned: it becomes `needs_review` with this reason.
    """
