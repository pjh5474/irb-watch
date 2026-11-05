"""Extract text from DOCX files (zipped WordprocessingML), read directly with no extra library.

Included: body paragraphs and tables, headers, footers, footnotes, endnotes, text boxes, and
inserted text from tracked changes. Left out: deleted text from tracked changes, comments,
field codes, and the duplicate "fallback" copy Word writes for text boxes.
"""

import io
import re

import olefile

from irb_watch.parsers._xmltext import xml_text
from irb_watch.parsers._zip import open_zip, read_part
from irb_watch.parsers.common import ParsedDocument, build_document, internal_error
from irb_watch.parsers.errors import ParseError, TextUnavailableError, UnreadableFileError

OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
BODY = "word/document.xml"
EXTRA_PARTS = re.compile(r"^word/(header\d*|footer\d*|footnotes|endnotes)\.xml$")

_TEXT = frozenset({"t"})
_PARAGRAPHS = frozenset({"p"})
_TABS = frozenset({"tab"})
_BREAKS = frozenset({"br", "cr"})
# del / moveFrom: deleted text. instrText: field codes. Fallback: duplicate of a text box.
_SKIP = frozenset({"del", "moveFrom", "delText", "instrText", "Fallback"})


def parse_docx(content: bytes) -> ParsedDocument:
    if content.startswith(OLE2_MAGIC):
        _reject_ole_container(content)
    try:
        archive = open_zip(content, "DOCX")
        names = set(archive.namelist())
        if BODY not in names:
            raise ParseError("not a valid docx file (word/document.xml is missing)")
        part_names = [BODY, *sorted(n for n in names if EXTRA_PARTS.match(n))]
        texts = [
            xml_text(
                read_part(archive, name, "DOCX"),
                text_tags=_TEXT,
                paragraph_tags=_PARAGRAPHS,
                tab_tags=_TABS,
                break_tags=_BREAKS,
                skip_tags=_SKIP,
                what="DOCX",
            )
            for name in part_names
        ]
        return build_document(texts, "DOCX")
    except (ParseError, UnreadableFileError, TextUnavailableError):
        raise
    except Exception as exc:
        raise internal_error("DOCX", exc) from exc


def _reject_ole_container(content: bytes) -> None:
    """A .docx that is an OLE container is a password-protected file (or an old .doc)."""
    try:
        with olefile.OleFileIO(io.BytesIO(content)) as ole:
            encrypted = ole.exists("EncryptedPackage")
    except Exception as exc:
        raise UnreadableFileError("DOCX is corrupt (unreadable container)") from exc
    if encrypted:
        raise UnreadableFileError("DOCX is password-protected")
    raise ParseError("not a valid docx file (it is an old binary Word file)")
