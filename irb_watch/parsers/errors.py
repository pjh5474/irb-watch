"""The outcomes when text cannot be extracted from a file.

Three different things can happen, and they are handled differently:

TextUnavailableError  The file is fine but yields no text, and will stay that way (scanned
                      PDF, DOCX-less formats, a distribution-protected HWP). Expected and
                      stable: tracked by file hash, labelled "text not available", no alert.

UnreadableFileError   A real document of the right type that we cannot read (corrupt,
                      password-protected, too large or complex, or a bug in our reader).
                      The post is still tracked and the file is tracked by hash, but it is
                      flagged `needs_review` with the reason so people can see why.

ParseError            The bytes are not the document the name promises (for example an HTML
                      login page served as .pdf). That is a download problem, not a property
                      of the document: the whole post fails and keeps its old state.
"""

NO_TEXT_LAYER = "no_text_layer"
UNSUPPORTED_FORMAT = "unsupported_format"
DISTRIBUTION_PROTECTED = "distribution_protected"
UNREADABLE = "unreadable"
TOO_LARGE = "too_large"  # an attachment over the size limit: listed, not downloaded
# (core/state.py FLAGGED_REASONS lists UNREADABLE and TOO_LARGE by value; keep them equal.)


class ParseError(Exception):
    """Bytes do not match the file type. Fails the post; never treated as empty or unchanged."""


class UnreadableFileError(Exception):
    """A genuine file we could not read. `note` is the reason shown to users."""

    def __init__(self, note: str) -> None:
        super().__init__(note)
        self.note = note


class TextUnavailableError(Exception):
    """The file is fine but yields no text. Track it by file hash only."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class UnsupportedFormatError(TextUnavailableError):
    """No text extractor for this file type yet (for example legacy .doc)."""

    def __init__(self, message: str) -> None:
        super().__init__(UNSUPPORTED_FORMAT, message)
