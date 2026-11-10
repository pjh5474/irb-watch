"""Turn one SNUH board into Observations for the change-detection logic.

Per post: read the detail page, download every attachment, hand the original to the file
sink (storage), extract text. Two kinds of trouble, handled differently:
  * the post cannot be read (layout change, download error, a "document" that is really an
    HTML error page): only that post fails and keeps its old state;
  * an attachment is a real file we cannot read (corrupt, password-protected, a bug): the post
    stays tracked, the file is tracked by hash, and a warning with the reason is attached.
"""

from dataclasses import dataclass, field
from datetime import datetime

from irb_watch.core.detect import Observation
from irb_watch.core.hashing import collapse_whitespace
from irb_watch.core.state import VersionContent
from irb_watch.crawlers.attachments import FileSink, observe_files
from irb_watch.crawlers.errors import CrawlError, LoginExpiredError, SiteUnavailableError
from irb_watch.crawlers.snuh import SnuhAdapter
from irb_watch.crawlers.window import newest
from irb_watch.parsers.errors import ParseError

__all__ = ["FileSink", "ScanReading", "SnuhSourceReader", "iso_date"]


@dataclass
class ScanReading:
    observations: list[Observation] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    unreadable_ids: set[str] = field(default_factory=set)
    declared_total: int = 0
    # Posts on the list that were not opened because they are older than the newest
    # MAX_DOCS_PER_URL (crawlers/window.py). Also in unreadable_ids, so never "missing".
    outside_window: int = 0


def pick_window(
    summaries: list, limit: int, reading: "ScanReading"
) -> list:
    """The posts to open: secret posts are skipped, then the newest `limit` of the rest. The
    others are marked unreadable on `reading` (neither seen nor missing)."""
    readable = []
    for summary in summaries:
        if summary.is_secret:
            reading.unreadable_ids.add(summary.post_id)
        else:
            readable.append(summary)
    chosen, rest = newest(readable, limit)
    reading.unreadable_ids.update(s.post_id for s in rest)
    reading.outside_window = len(rest)
    return chosen


def iso_date(site_date: str) -> str | None:
    """'2026.09.03' -> '2026-09-03'; None if the site shows something else."""
    try:
        return datetime.strptime(site_date.strip(), "%Y.%m.%d").date().isoformat()
    except ValueError:
        return None


class SnuhSourceReader:
    def __init__(
        self, adapter: SnuhAdapter, board_key: str, max_posts: int, max_file_bytes: int
    ) -> None:
        self._adapter = adapter
        self._board_key = board_key
        self._max_posts = max_posts
        self._max_file_bytes = max_file_bytes

    def close(self) -> None:
        self._adapter.close()

    def read(self, file_sink: FileSink) -> ScanReading:
        """List errors (layout change, incomplete list, network) propagate: the whole scan fails."""
        declared_total, summaries = self._adapter.list_board(self._board_key)
        reading = ScanReading(declared_total=declared_total)
        for summary in pick_window(summaries, self._max_posts, reading):
            try:
                reading.observations.append(self._observe(summary, file_sink))
            except (LoginExpiredError, SiteUnavailableError):
                # The session died or the site is down: fail the whole scan once, with one
                # reason, instead of flagging every remaining post.
                raise
            except (CrawlError, ParseError) as exc:
                reading.failures[summary.post_id] = str(exc)
        return reading

    def _observe(self, summary, file_sink: FileSink) -> Observation:
        detail = self._adapter.fetch_post(summary)
        # The page text repeats the title; keep it out so a title edit is not also a text edit.
        body = collapse_whitespace(detail.body_text.replace(summary.title, " ", 1))

        # A zip becomes the zip plus each file inside; unreadable files still count by hash,
        # with a warning so people see why there is no text.
        files, warnings = observe_files(
            detail.attachments, self._adapter.download, self._max_file_bytes, file_sink
        )
        return Observation(
            external_id=summary.post_id,
            source_url=summary.detail_url,
            posted_on=iso_date(summary.posted_on),
            content=VersionContent(summary.title, body, tuple(files)),
            warnings=tuple(warnings),
        )
