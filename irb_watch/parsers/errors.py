"""Two different outcomes when text cannot be extracted from a file.

ParseError: something is wrong (corrupt, password-protected, too big, not what it claims
to be). The pipeline marks the source `needs_review`.

TextUnavailableError: expected and stable (scanned image PDF, HWP/DOCX with no extractor
yet). It is deliberately NOT a ParseError, so `except ParseError` cannot swallow it. The
file is tracked by its file hash only and labelled "text not available"; it must not raise
an alert on every scan.
"""

NO_TEXT_LAYER = "no_text_layer"
UNSUPPORTED_FORMAT = "unsupported_format"


class ParseError(Exception):
    """A file could not be read. Mark `needs_review`; never treat as empty or unchanged."""


class TextUnavailableError(Exception):
    """The file is fine but yields no text. Track it by file hash only."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class UnsupportedFormatError(TextUnavailableError):
    """No text extractor for this file type yet (for example HWP or DOCX)."""

    def __init__(self, message: str) -> None:
        super().__init__(UNSUPPORTED_FORMAT, message)
