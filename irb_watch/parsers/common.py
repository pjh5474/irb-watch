"""Shared by every format reader: the result type and the final text checks."""

import logging
from dataclasses import dataclass

from irb_watch.core.hashing import normalize_text, sha256_hex
from irb_watch.parsers.errors import NO_TEXT_LAYER, TextUnavailableError, UnreadableFileError

log = logging.getLogger("irb_watch.parsers")

MIN_TEXT_CHARS = 20  # below this the file is treated as having no text
MAX_TEXT_CHARS = 5_000_000  # a bigger result is refused, never silently cut
MAX_UNCOMPRESSED_BYTES = 100_000_000  # zip / deflate bomb guard


@dataclass(frozen=True)
class ParsedDocument:
    text: str  # normalized (see core.hashing.normalize_text)
    text_hash: str
    pages: int  # pages for PDF; sections or parts for other formats
    empty_pages: int  # pages or parts without any text


def build_document(part_texts: list[str], what: str) -> ParsedDocument:
    """Normalize the text of each page or part and apply the common size checks."""
    if not part_texts:
        raise UnreadableFileError(f"{what} has no readable content")
    normalized = [normalize_text(t) for t in part_texts]
    text = normalize_text(" ".join(normalized))
    if len(text) > MAX_TEXT_CHARS:
        raise UnreadableFileError(f"{what} text is larger than {MAX_TEXT_CHARS:,} characters")
    if len(text) < MIN_TEXT_CHARS:
        raise TextUnavailableError(NO_TEXT_LAYER, f"{what} has no extractable text")
    return ParsedDocument(
        text=text,
        text_hash=sha256_hex(text.encode("utf-8")),
        pages=len(part_texts),
        empty_pages=sum(1 for t in normalized if not t),
    )


def internal_error(what: str, exc: Exception) -> UnreadableFileError:
    """A bug or an unexpected file shape in our reader: recorded in the log with a traceback
    and reported to users as a short, honest note."""
    log.error("unexpected error while reading %s", what, exc_info=exc)
    return UnreadableFileError(f"internal error while reading {what} ({type(exc).__name__})")
