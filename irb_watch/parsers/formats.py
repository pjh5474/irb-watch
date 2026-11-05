"""Pick a parser by what the file really is (magic bytes first, extension second)."""

from dataclasses import dataclass

from irb_watch.parsers.errors import ParseError, TextUnavailableError, UnsupportedFormatError
from irb_watch.parsers.pdf import ParsedDocument, parse_pdf

# A document extension promises a content type; if the bytes disagree it is a failure.
_EXPECTED_BY_EXTENSION = {"pdf": "pdf", "hwp": "hwp", "hwpx": "hwpx", "docx": "docx"}

_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy HWP, DOC, XLS
_ZIP = b"PK\x03\x04"  # DOCX, HWPX, XLSX


@dataclass(frozen=True)
class ExtractResult:
    """Outcome of reading one file. Real failures are not here: they raise ParseError."""

    document: ParsedDocument | None  # set when text was extracted
    hash_only_reason: str | None  # set when the file is tracked by file hash alone

    @property
    def has_text(self) -> bool:
        return self.document is not None


def detect_format(filename: str, content: bytes) -> str:
    """Return 'pdf', 'hwp', 'hwpx', 'docx' or 'unknown'."""
    name = filename.lower()
    if content.startswith(b"%PDF-"):
        return "pdf"
    if content.startswith(_OLE2) and name.endswith(".hwp"):
        return "hwp"
    if content.startswith(_ZIP):
        if name.endswith(".hwpx"):
            return "hwpx"
        if name.endswith(".docx"):
            return "docx"
    return "unknown"


def parse_document(filename: str, content: bytes) -> ParsedDocument:
    """Extract text. Raises ParseError (failure) or TextUnavailableError (hash-only)."""
    kind = detect_format(filename, content)
    expected = _EXPECTED_BY_EXTENSION.get(filename.lower().rpartition(".")[2])
    if expected is not None and kind != expected:
        # Typically a login or error page served with status 200 under a document name.
        raise ParseError(f"{filename}: content is not a valid {expected} file")
    if kind == "pdf":
        return parse_pdf(content)
    raise UnsupportedFormatError(f"no text extractor for {kind} files yet: {filename}")


def extract_text(filename: str, content: bytes) -> ExtractResult:
    """Pipeline entry point: text if available, else a hash-only marker.

    Still raises ParseError for real failures, which the caller maps to `needs_review`.
    """
    try:
        return ExtractResult(parse_document(filename, content), None)
    except TextUnavailableError as exc:
        return ExtractResult(None, exc.reason)
