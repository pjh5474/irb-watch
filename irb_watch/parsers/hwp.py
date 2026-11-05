"""Extract body text from HWP 5.x files (Hancom's binary format) with olefile and zlib.

Written against the public HWP 5.0 file-format description, so the license stays permissive
(the popular pyhwp library is AGPL). Only text is read: the paragraph-text records of every
BodyText section, which includes table cells, headers, footers and footnotes. Pictures,
equations and embedded objects are ignored.

Outcomes
  readable            text
  password-protected  UnreadableFileError (an admin should look at it)
  DRM / certificate   UnreadableFileError
  distribution doc    TextUnavailableError: the text is scrambled on purpose and stays so
  HWP before 5.0      TextUnavailableError (unsupported format)
"""

import io
import zlib

import olefile

from irb_watch.parsers.common import (
    MAX_UNCOMPRESSED_BYTES,
    ParsedDocument,
    build_document,
    internal_error,
)
from irb_watch.parsers.errors import (
    DISTRIBUTION_PROTECTED,
    UNSUPPORTED_FORMAT,
    ParseError,
    TextUnavailableError,
    UnreadableFileError,
)

SIGNATURE = b"HWP Document File"
FLAG_COMPRESSED = 1 << 0
FLAG_PASSWORD = 1 << 1
FLAG_DISTRIBUTION = 1 << 2
FLAG_DRM = 1 << 4
FLAG_CERTIFICATE_ENCRYPTED = 1 << 8
FLAG_CERTIFICATE_DRM = 1 << 10

TAG_PARA_TEXT = 67  # HWPTAG_BEGIN (16) + 51
MAX_SECTIONS = 1000

# Control characters below 32. Most occupy 8 two-byte units: the character plus 14 bytes of
# data that is not text (fields, tabs, drawing objects, section and column definitions...).
_EIGHT_UNIT_CONTROLS = frozenset(
    {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}
)
_TAB = 9
_LINE_BREAK = 10
_PARAGRAPH_END = 13
_HYPHEN = 24
_FIXED_SPACES = frozenset({30, 31})


def parse_hwp(content: bytes) -> ParsedDocument:
    try:
        return _parse(content)
    except (ParseError, UnreadableFileError, TextUnavailableError):
        raise
    except Exception as exc:
        raise internal_error("HWP", exc) from exc


def _parse(content: bytes) -> ParsedDocument:
    try:
        ole = olefile.OleFileIO(io.BytesIO(content))
    except Exception as exc:
        raise UnreadableFileError("HWP is corrupt (unreadable container)") from exc

    with ole:
        if not ole.exists("FileHeader"):
            raise ParseError("not a valid hwp file (no FileHeader stream)")
        header = ole.openstream("FileHeader").read(256)
        if not header.startswith(SIGNATURE) or len(header) < 40:
            raise ParseError("not a valid hwp file (wrong signature)")

        major_version = header[35]  # the version is a little-endian DWORD: 0xMMnnPPrr
        flags = int.from_bytes(header[36:40], "little")
        if major_version != 5:
            raise TextUnavailableError(
                UNSUPPORTED_FORMAT, f"HWP version {major_version}.x has no text reader"
            )
        if flags & FLAG_PASSWORD:
            raise UnreadableFileError("HWP is password-protected")
        if flags & (FLAG_DRM | FLAG_CERTIFICATE_ENCRYPTED | FLAG_CERTIFICATE_DRM):
            raise UnreadableFileError("HWP is protected by DRM or certificate encryption")
        if flags & FLAG_DISTRIBUTION:
            raise TextUnavailableError(
                DISTRIBUTION_PROTECTED, "distribution document: its text is scrambled on purpose"
            )

        compressed = bool(flags & FLAG_COMPRESSED)
        sections = _section_streams(ole)
        if not sections:
            raise UnreadableFileError("HWP has no body text sections")
        texts = [_section_text(ole, path, compressed) for path in sections]
    return build_document(texts, "HWP")


def _section_streams(ole: olefile.OleFileIO) -> list[list[str]]:
    found = []
    for path in ole.listdir():
        if len(path) == 2 and path[0] == "BodyText" and path[1].startswith("Section"):
            suffix = path[1][len("Section") :]
            if suffix.isdigit():
                found.append((int(suffix), path))
    return [path for _, path in sorted(found)][:MAX_SECTIONS]


def _section_text(ole: olefile.OleFileIO, path: list[str], compressed: bool) -> str:
    if ole.get_size(path) > MAX_UNCOMPRESSED_BYTES:
        raise UnreadableFileError("HWP section is too large")
    raw = ole.openstream(path).read()
    if compressed:
        inflater = zlib.decompressobj(-zlib.MAX_WBITS)  # HWP stores raw deflate data
        try:
            data = inflater.decompress(raw, MAX_UNCOMPRESSED_BYTES + 1)
        except zlib.error as exc:
            raise UnreadableFileError("HWP section is corrupt (cannot decompress)") from exc
        if len(data) > MAX_UNCOMPRESSED_BYTES:
            raise UnreadableFileError("HWP section expands to more than the allowed size")
    else:
        data = raw
    return "\n".join(_paragraphs(data))


def _paragraphs(data: bytes):
    """Yield the text of every paragraph record (PARA_TEXT) in a section stream."""
    pos, end = 0, len(data)
    while pos + 4 <= end:
        header = int.from_bytes(data[pos : pos + 4], "little")
        pos += 4
        tag = header & 0x3FF
        size = header >> 20
        if size == 0xFFF:  # the real size follows in the next four bytes
            if pos + 4 > end:
                raise UnreadableFileError("HWP section is truncated")
            size = int.from_bytes(data[pos : pos + 4], "little")
            pos += 4
        if pos + size > end:
            raise UnreadableFileError("HWP section is truncated")
        if tag == TAG_PARA_TEXT:
            yield _paragraph_text(data[pos : pos + size])
        pos += size


def _paragraph_text(payload: bytes) -> str:
    out = bytearray()
    i, n = 0, len(payload) - len(payload) % 2
    while i < n:
        unit = int.from_bytes(payload[i : i + 2], "little")
        i += 2
        if unit >= 32:
            out += payload[i - 2 : i]
        elif unit in _EIGHT_UNIT_CONTROLS:
            if unit == _TAB:
                out += b"\t\x00"
            i += 14  # the rest of this control: not text
        elif unit in (_LINE_BREAK, _PARAGRAPH_END):
            out += b"\n\x00"
        elif unit == _HYPHEN:
            out += b"-\x00"
        elif unit in _FIXED_SPACES:
            out += b" \x00"
        # any other control character carries no text
    return out.decode("utf-16-le", errors="replace")
