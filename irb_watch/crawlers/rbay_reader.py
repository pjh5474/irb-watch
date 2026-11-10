"""Turn one R-BAY board into Observations for the change-detection logic.

Same failure rules as the SNUH reader: a list problem fails the whole scan; one broken post
fails only itself; an unreadable file keeps the post tracked with a warning. A session that
ends mid-scan (or a site that is down) fails the scan once, with one reason, except that a
session handed over from an earlier board may already have timed out at R-BAY: then the board
is read again once with a fresh login.
"""

from collections.abc import Callable

from irb_watch.core.detect import Observation
from irb_watch.core.state import VersionContent
from irb_watch.crawlers.attachments import FileSink, observe_files
from irb_watch.crawlers.credentials import CredentialStore
from irb_watch.crawlers.errors import CrawlError, LoginExpiredError, SiteUnavailableError
from irb_watch.crawlers.fetcher import SafeFetcher
from irb_watch.crawlers.rbay import RbayAdapter
from irb_watch.crawlers.rbay_login import RbaySession, RbaySessionPool
from irb_watch.crawlers.snuh_reader import ScanReading
from irb_watch.parsers.errors import ParseError


class RbaySourceReader:
    def __init__(
        self,
        pool: RbaySessionPool,
        credentials: CredentialStore,
        user: str,
        agency: str,
        board: str,
        max_posts: int,
        max_file_bytes: int,
        make_fetcher: Callable[[RbaySession], SafeFetcher],
    ) -> None:
        self._pool = pool
        self._credentials = credentials
        self._user = user
        self._agency = agency
        self._board = board
        self._max_posts = max_posts
        self._max_file_bytes = max_file_bytes
        self._make_fetcher = make_fetcher
        self._person_started = False

    def person_started(self) -> None:
        """Called for a scan a person started: a remembered rejected login may be tried again."""
        self._person_started = True

    def close(self) -> None:
        """Nothing to close: read() returns the session to the pool when it ends."""

    def read(self, file_sink: FileSink) -> ScanReading:
        lease = self._pool.acquire(self._user, self._agency, self._credentials, self._person_started)
        try:
            return self._read_with(lease.session, file_sink)
        except LoginExpiredError:
            if lease.fresh:
                raise  # a session from this very login stopped working: report it
            # Handed over from an earlier board and already ended at R-BAY: log in once more.
            lease = self._pool.acquire(self._user, self._agency, self._credentials, self._person_started)
            return self._read_with(lease.session, file_sink)

    def _read_with(self, session: RbaySession, file_sink: FileSink) -> ScanReading:
        adapter = RbayAdapter(self._make_fetcher(session), self._agency, session.menus)
        try:
            return self._read(adapter, file_sink)
        except LoginExpiredError:
            self._pool.discard(session)
            raise
        finally:
            adapter.close()
            self._pool.release(session)

    def _read(self, adapter: RbayAdapter, file_sink: FileSink) -> ScanReading:
        declared, summaries = adapter.list_board(self._board, self._max_posts)
        reading = ScanReading(declared_total=declared)
        for summary in summaries:
            try:
                reading.observations.append(self._observe(adapter, summary, file_sink))
            except (LoginExpiredError, SiteUnavailableError):
                raise
            except (CrawlError, ParseError) as exc:
                reading.failures[summary.post_id] = str(exc)
        return reading

    def _observe(self, adapter: RbayAdapter, summary, file_sink: FileSink) -> Observation:
        detail = adapter.fetch_post(summary)
        files, warnings = observe_files(detail.attachments, adapter.download, self._max_file_bytes, file_sink)
        return Observation(
            external_id=summary.post_id,
            source_url=summary.detail_url,
            posted_on=summary.posted_on,  # already YYYY-MM-DD on R-BAY
            content=VersionContent(summary.title, detail.body_text, tuple(files)),
            warnings=tuple(warnings),
        )
