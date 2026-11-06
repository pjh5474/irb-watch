import hashlib
import re
import unicodedata

_WS = re.compile(r"\s+")
# Invisible characters that PDFs and web pages sprinkle in; they must not affect a hash.
# Written as code points on purpose: literal invisible characters are unreviewable.
#   200B-200D zero-width space/non-joiner/joiner, 2060 word joiner, FEFF BOM, 00AD soft hyphen
_INVISIBLE = dict.fromkeys([0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD])
_NBSP = chr(0x00A0)
_VIEW_COUNT = re.compile(r"조회\s*[\d,]+")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def collapse_whitespace(text: str) -> str:
    # A NUL character (code point 0) cannot be stored in a Postgres text column; some
    # extracted file text contains it (found by the first live scan of a login board).
    return _WS.sub(" ", text.replace(chr(0), "")).strip()


def normalize_text(text: str) -> str:
    """Canonical form for hashing extracted text.

    NFC keeps composed Korean syllables identical whichever way the source encoded them;
    invisible characters are dropped and all whitespace collapses to single spaces.
    """
    text = unicodedata.normalize("NFC", text).translate(_INVISIBLE)
    return collapse_whitespace(text.replace(_NBSP, " "))


def strip_view_count(text: str) -> str:
    """The view counter changes on every visit and must never reach a hash."""
    return _VIEW_COUNT.sub("", text)
