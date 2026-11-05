"""Turn one SNUH board into Observations for the change-detection logic.

Per post: read the detail page, download every attachment, hand the original to the file
sink (storage), extract text. Two kinds of trouble, handled differently:
  * the post cannot be read (layout change, download error, a "document" that is really an
    HTML error page): only that post fails and keeps its old state;
  * an attachment is a real file we cannot read (corrupt, password-protected, a bug): the post
    stays tracked, the file is tracked by hash, and a warning with the reason is attached.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from irb_watch.core.detect import Observation
from irb_watch.core.hashing import collapse_whitespace
from irb_watch.core.state import FileContent, VersionContent
from irb_watch.crawlers.errors import CrawlError
from irb_watch.crawlers.snuh import SnuhAdapter
from irb_watch.parsers.errors import ParseError
from irb_watch.parsers.formats import extract_text

# file_sink(sha256, filename, content) stores the original and returns its storage path.
FileSink = Callable[[str, str, bytes], str]


@dataclass
class ScanReading:
    observations: list[Observation] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    unreadable_ids: set[str] = field(default_factory=set)
    declared_total: int = 0


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
        declared_total, summaries = self._adapter.list_board(self._board_key, self._max_posts)
        reading = ScanReading(declared_total=declared_total)
        for summary in summaries:
            if summary.is_secret:
                reading.unreadable_ids.add(summary.post_id)
                continue
            try:
                reading.observations.append(self._observe(summary, file_sink))
            except (CrawlError, ParseError) as exc:
                reading.failures[summary.post_id] = str(exc)
        return reading

    def _observe(self, summary, file_sink: FileSink) -> Observation:
        detail = self._adapter.fetch_post(summary)
        # The page text repeats the title; keep it out so a title edit is not also a text edit.
        body = collapse_whitespace(detail.body_text.replace(summary.title, " ", 1))

        files: list[FileContent] = []
        warnings: list[str] = []
        seen_names: dict[str, int] = {}
        for ref in detail.attachments:
            downloaded = self._adapter.download(ref, self._max_file_bytes)
            extracted = extract_text(downloaded.filename, downloaded.content)
            storage_path = file_sink(downloaded.sha256, downloaded.filename, downloaded.content)
            count = seen_names.get(ref.filename, 0) + 1
            seen_names[ref.filename] = count
            name = ref.filename if count == 1 else f"{ref.filename} ({count})"
            files.append(
                FileContent(
                    filename=name,
                    size_label=ref.size_label,
                    sha256=downloaded.sha256,
                    text=extracted.document.text if extracted.document else None,
                    hash_only_reason=extracted.hash_only_reason,
                    storage_path=storage_path,
                    note=extracted.note,
                )
            )
            if extracted.unreadable:
                # Still tracked by file hash, but people need to see why there is no text.
                warnings.append(f"{name}: {extracted.note}")

        return Observation(
            external_id=summary.post_id,
            source_url=summary.detail_url,
            posted_on=iso_date(summary.posted_on),
            content=VersionContent(summary.title, body, tuple(files)),
            warnings=tuple(warnings),
        )
