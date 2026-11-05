"""Pick a reader by what the file really is (magic bytes first, extension second)."""

from dataclasses import dataclass

from irb_watch.parsers.common import ParsedDocument
from irb_watch.parsers.docx import parse_docx
from irb_watch.parsers.errors import (
    UNREADABLE,
    ParseError,
    TextUnavailableError,
    UnreadableFileError,
    UnsupportedFormatError,
)
from irb_watch.parsers.hwp import parse_hwp
from irb_watch.parsers.hwpx import parse_hwpx
from irb_watch.parsers.pdf import parse_pdf

# A document extension promises a content type; if the bytes disagree it is a failure.
_EXPECTED_BY_EXTENSION = {"pdf": "pdf", "hwp": "hwp", "hwpx": "hwpx", "docx": "docx"}

_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy HWP, DOC, XLS, encrypted Office files
_ZIP = b"PK\x03\x04"  # DOCX, HWPX, XLSX

_READERS = {"pdf": parse_pdf, "hwp": parse_hwp, "hwpx": parse_hwpx, "docx": parse_docx}


@dataclass(frozen=True)
class ExtractResult:
    """Outcome of reading one file. Only a mismatch between name and bytes raises (ParseError)."""

    document: ParsedDocument | None  # set when text was extracted
    hash_only_reason: str | None  # set when the file is tracked by file hash alone
    note: str | None = None  # human-readable detail for hash-only files

    @property
    def has_text(self) -> bool:
        return self.document is not None

    @property
    def unreadable(self) -> bool:
        return self.hash_only_reason == UNREADABLE


def detect_format(filename: str, content: bytes) -> str:
    """Return 'pdf', 'hwp', 'hwpx', 'docx' or 'unknown'."""
    name = filename.lower()
    if content.startswith(b"%PDF-"):
        return "pdf"
    if content.startswith(_OLE2):
        if name.endswith(".hwp"):
            return "hwp"
        if name.endswith(".docx"):
            return "docx"  # an OLE container named .docx is usually a password-protected file
    if content.startswith(_ZIP):
        if name.endswith(".hwpx"):
            return "hwpx"
        if name.endswith(".docx"):
            return "docx"
    return "unknown"


def parse_document(filename: str, content: bytes) -> ParsedDocument:
    """Extract text. May raise ParseError, UnreadableFileError or TextUnavailableError."""
    kind = detect_format(filename, content)
    expected = _EXPECTED_BY_EXTENSION.get(filename.lower().rpartition(".")[2])
    if expected is not None and kind != expected:
        # Typically a login or error page served with status 200 under a document name.
        raise ParseError(f"{filename}: content is not a valid {expected} file")
    reader = _READERS.get(kind)
    if reader is None:
        raise UnsupportedFormatError(f"no text extractor for {kind} files yet: {filename}")
    return reader(content)


def extract_text(filename: str, content: bytes) -> ExtractResult:
    """Pipeline entry point.

    Returns text, or a hash-only marker (expected: scanned, protected, unsupported; or
    unreadable: corrupt, password-protected, bug) with a note users can read. Raises
    ParseError only when the bytes are not the document the name promises.
    """
    try:
        return ExtractResult(parse_document(filename, content), None)
    except TextUnavailableError as exc:
        return ExtractResult(None, exc.reason, str(exc))
    except UnreadableFileError as exc:
        return ExtractResult(None, UNREADABLE, exc.note)
