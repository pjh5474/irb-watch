import hashlib
import re

_WS = re.compile(r"\s+")
_VIEW_COUNT = re.compile(r"조회\s*[\d,]+")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def collapse_whitespace(text: str) -> str:
    return _WS.sub(" ", text).strip()


def strip_view_count(text: str) -> str:
    """The view counter changes on every visit and must never reach a hash."""
    return _VIEW_COUNT.sub("", text)
