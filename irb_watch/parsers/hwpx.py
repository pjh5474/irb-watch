"""Extract text from HWPX files (Hancom's zipped-XML format)."""

import re

from irb_watch.parsers._xmltext import xml_text
from irb_watch.parsers._zip import open_zip, read_part
from irb_watch.parsers.common import ParsedDocument, build_document, internal_error
from irb_watch.parsers.errors import ParseError, TextUnavailableError, UnreadableFileError

SECTION = re.compile(r"^Contents/section(\d+)\.xml$")
MANIFEST = "META-INF/manifest.xml"

_TEXT = frozenset({"t"})
_PARAGRAPHS = frozenset({"p"})
_TABS = frozenset({"tab"})
_BREAKS = frozenset({"lineBreak"})


def parse_hwpx(content: bytes) -> ParsedDocument:
    try:
        archive = open_zip(content, "HWPX")
        names = archive.namelist()
        sections = sorted(
            (n for n in names if SECTION.match(n)), key=lambda n: int(SECTION.match(n).group(1))
        )
        if not sections:
            raise ParseError("not a valid hwpx file (no Contents/section files)")
        if MANIFEST in names and b"encryption-data" in read_part(archive, MANIFEST, "HWPX"):
            raise UnreadableFileError("HWPX is password-protected")
        texts = [
            xml_text(
                read_part(archive, name, "HWPX"),
                text_tags=_TEXT,
                paragraph_tags=_PARAGRAPHS,
                tab_tags=_TABS,
                break_tags=_BREAKS,
                what="HWPX",
            )
            for name in sections
        ]
        return build_document(texts, "HWPX")
    except (ParseError, UnreadableFileError, TextUnavailableError):
        raise
    except Exception as exc:
        raise internal_error("HWPX", exc) from exc
