"""Turn one R-BAY board into Observations for the change-detection logic.

Same failure rules as the SNUH reader: a list problem fails the whole scan; one broken post
fails only itself; an unreadable file keeps the post tracked with a warning. A session that
ends mid-scan (or a site that is down) fails the scan once, with one reason, except that a
session handed over from an earlier board may already have timed out at R-BAY: then the board
is read again once with a fresh login.
"""

import logging
from collections.abc import Callable

from irb_watch.core.detect import Observation
from irb_watch.core.state import VersionContent
from irb_watch.crawlers.attachments import FileSink, observe_files
from irb_watch.crawlers.credentials import CredentialStore
from irb_watch.crawlers.errors import CrawlError, LoginExpiredError, SiteUnavailableError
from irb_watch.crawlers.fetcher import SafeFetcher
from irb_watch.crawlers.rbay import MAX_TEXT_IMAGES, RbayAdapter
from irb_watch.crawlers.rbay_login import RbaySession, RbaySessionPool
from irb_watch.crawlers.snuh_reader import ScanReading, pick_backlog, pick_window
from irb_watch.parsers.errors import ParseError

log = logging.getLogger(__name__)


class RbaySourceReader:
    def __init__(
        self,
        pool: RbaySessionPool,
        credentials: CredentialStore,
        user: str,
        agency: str,
        site: str,
        board: str,
        max_posts: int,
        max_file_bytes: int,
        make_fetcher: Callable[[RbaySession], SafeFetcher],
        refresh_contacts: Callable[[RbayAdapter], None] | None = None,
    ) -> None:
        self._pool = pool
        self._credentials = credentials
        self._user = user
        self._agency = agency
        self._site = site  # where this hospital's login is stored
        self._board = board
        self._max_posts = max_posts
        self._max_file_bytes = max_file_bytes
        self._make_fetcher = make_fetcher
        # Reads the hospital's contact cards once per run, in this session (see services/contacts).
        self._refresh_contacts = refresh_contacts
        self._person_started = False

    def person_started(self) -> None:
        """Called for a scan a person started: a remembered rejected login may be tried again."""
        self._person_started = True

    def close(self) -> None:
        """Nothing to close: read() returns the session to the pool when it ends."""

    def _acquire(self):
        return self._pool.acquire(
            self._user, self._agency, self._site, self._credentials, self._person_started
        )

    def read(self, file_sink: FileSink) -> ScanReading:
        return self._with_session(lambda adapter: self._read(adapter, file_sink))

    def read_backlog(
        self, file_sink: FileSink, stored_ids: frozenset[str], limit: int, should_stop: Callable[[], bool]
    ) -> ScanReading:
        """A batch of the board's posts that are not stored yet (ALL-SEARCH): the newest `limit`.
        Same login rules as a scan (the hospital's pooled session); the contact cards are left
        alone. `should_stop` is checked before each post; the ones not opened stay unread."""
        return self._with_session(
            lambda adapter: self._read_backlog(adapter, file_sink, stored_ids, limit, should_stop), contacts=False
        )

    def _with_session(self, work: Callable[[RbayAdapter], ScanReading], contacts: bool = True) -> ScanReading:
        lease = self._acquire()
        try:
            return self._read_with(lease.session, work, contacts)
        except LoginExpiredError:
            if lease.fresh:
                raise  # a session from this very login stopped working: report it
            # Handed over from an earlier board and already ended at R-BAY: log in once more.
            return self._read_with(self._acquire().session, work, contacts)

    def _read_with(
        self, session: RbaySession, work: Callable[[RbayAdapter], ScanReading], contacts: bool = True
    ) -> ScanReading:
        adapter = RbayAdapter(self._make_fetcher(session), self._agency, session.menus)
        try:
            reading = work(adapter)
            if contacts:
                self._contacts(adapter)  # after the board read fine: the session is known to be good
            return reading
        except LoginExpiredError:
            self._pool.discard(session)
            raise
        finally:
            adapter.close()
            self._pool.release(session)

    def _contacts(self, adapter: RbayAdapter) -> None:
        """The contact cards. Whatever happens here never fails or changes the board's scan: the
        callback records its own result, and an error that still escapes is only logged."""
        if self._refresh_contacts is None:
            return
        try:
            self._refresh_contacts(adapter)
        except Exception as exc:  # noqa: BLE001 - not a reason to fail the scan or end the session
            log.warning("contact cards of %s were not read: %s", self._site, type(exc).__name__)

    def _read(self, adapter: RbayAdapter, file_sink: FileSink) -> ScanReading:
        declared, summaries = adapter.list_board(self._board)
        reading = ScanReading(declared_total=declared)
        for summary in pick_window(summaries, self._max_posts, reading):
            try:
                reading.observations.append(self._observe(adapter, summary, file_sink))
            except (LoginExpiredError, SiteUnavailableError):
                raise
            except (CrawlError, ParseError) as exc:
                reading.failures[summary.post_id] = str(exc)
        return reading

    def _read_backlog(
        self, adapter: RbayAdapter, file_sink: FileSink, stored_ids: frozenset[str], limit: int,
        should_stop: Callable[[], bool],
    ) -> ScanReading:
        declared, summaries = adapter.list_board(self._board)
        reading = ScanReading(declared_total=declared)
        chosen = pick_backlog(summaries, stored_ids, limit, reading)
        for i, summary in enumerate(chosen):
            if should_stop():
                reading.unreadable_ids.update(s.post_id for s in chosen[i:])
                break
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
        if detail.skipped_images:
            warnings.append(
                f"{detail.skipped_images} more images in the post's text were not saved "
                f"(at most {MAX_TEXT_IMAGES} per post)"
            )
        return Observation(
            external_id=summary.post_id,
            source_url=summary.detail_url,
            posted_on=summary.posted_on,  # already YYYY-MM-DD on R-BAY
            content=VersionContent(summary.title, detail.body_text, tuple(files)),
            warnings=tuple(warnings),
        )
