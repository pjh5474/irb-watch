"""Open zip-based documents (DOCX, HWPX) defensively."""

import zipfile
from io import BytesIO

from irb_watch.parsers.common import MAX_UNCOMPRESSED_BYTES
from irb_watch.parsers.errors import UnreadableFileError

MAX_ENTRIES = 10_000
MAX_PART_BYTES = 50_000_000


def open_zip(content: bytes, what: str) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise UnreadableFileError(f"{what} is corrupt (not a valid zip archive)") from exc

    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise UnreadableFileError(f"{what} has too many parts ({len(infos)})")
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_BYTES:
        raise UnreadableFileError(f"{what} expands to more than the allowed size")
    if any(i.flag_bits & 0x1 for i in infos):
        raise UnreadableFileError(f"{what} is password-protected")
    return archive


def read_part(archive: zipfile.ZipFile, name: str, what: str) -> bytes:
    """Read one part, never more than MAX_PART_BYTES (the header size can lie)."""
    try:
        with archive.open(name) as part:
            data = part.read(MAX_PART_BYTES + 1)
    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError) as exc:
        raise UnreadableFileError(f"{what} part {name} is corrupt") from exc
    if len(data) > MAX_PART_BYTES:
        raise UnreadableFileError(f"{what} part {name} is too large")
    return data
