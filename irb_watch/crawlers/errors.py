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
