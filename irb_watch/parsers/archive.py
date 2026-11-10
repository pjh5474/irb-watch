"""Unpack a .zip attachment into its files, in memory, with hard limits.

Decided with the owner (2026-11-10): the zip itself is kept, and every file inside becomes its
own stored, downloadable item with its text read. Only the top-level zip is unpacked; a zip
inside a zip is kept as one file. Nothing is written to disk, so path tricks in member names
("../x") cannot escape anything; names are still cleaned for display.

Members are read one at a time (iter_zip is a generator), so only one unpacked file is in
memory at once. Limits against zip bombs: at most MAX_FILES files, MAX_TOTAL_BYTES unpacked in
total, and each file at most max_member_bytes (the same cap as a normal download). The sizes a
zip declares are checked first and every member is also read with a running cap, because those
sizes can lie. Over a limit, an encrypted member or a damaged zip -> ArchiveError with a reason.
"""

import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO

MAX_FILES = 200
MAX_TOTAL_BYTES = 200_000_000
_CHUNK = 1 << 20
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")  # a normal zip, an empty zip
# Folders macOS adds to zips; not documents.
_SKIP_PREFIXES = ("__MACOSX/",)
_SKIP_NAMES = (".DS_Store", "Thumbs.db", "desktop.ini")


class ArchiveError(Exception):
    """The zip cannot be unpacked within the rules. The message is shown to users."""


@dataclass(frozen=True)
class Member:
    name: str  # path inside the zip, cleaned, "/"-separated
    content: bytes


def is_zip(filename: str, content: bytes) -> bool:
    """A .zip by name AND by bytes. (DOCX, HWPX and XLSX are zips too, but not by name.)"""
    return filename.lower().endswith(".zip") and content[:4] in _ZIP_MAGIC


def _decoded_name(info: zipfile.ZipInfo) -> str:
    """Korean Windows tools write CP949 names without the UTF-8 flag; Python then decodes them
    as CP437. Undo that when the flag is missing."""
    name = info.filename
    if not info.flag_bits & 0x800:
        try:
            name = name.encode("cp437").decode("cp949")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name


def _clean(name: str) -> str:
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


def _mb(n: int) -> str:
    return f"{n // 1_000_000} MB"


def iter_zip(
    content: bytes,
    max_member_bytes: int,
    max_files: int = MAX_FILES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> Iterator[Member]:
    """Yield the zip's files one by one. Raises ArchiveError (before the first file when the
    zip's own sizes already break a limit, otherwise as soon as a limit is crossed)."""
    try:
        archive = zipfile.ZipFile(BytesIO(content))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise ArchiveError(f"the zip file is damaged ({exc})") from None

    with archive:
        infos = []
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = _clean(_decoded_name(info))
            if not name or name.startswith(_SKIP_PREFIXES) or name.rsplit("/", 1)[-1] in _SKIP_NAMES:
                continue
            infos.append((info, name))
        if len(infos) > max_files:
            raise ArchiveError(f"the zip holds {len(infos)} files; at most {max_files} are unpacked")
        if any(info.flag_bits & 0x1 for info, _ in infos):
            raise ArchiveError("the zip is password-protected; its files cannot be read")
        too_big = next((name for info, name in infos if info.file_size > max_member_bytes), None)
        if too_big:
            raise ArchiveError(f"{too_big} inside the zip is larger than {_mb(max_member_bytes)}")
        if sum(info.file_size for info, _ in infos) > max_total_bytes:
            raise ArchiveError(f"the zip unpacks to more than {_mb(max_total_bytes)}; not unpacked")

        total = 0
        for info, name in infos:
            chunks: list[bytes] = []
            size = 0
            try:
                with archive.open(info) as stream:
                    while chunk := stream.read(_CHUNK):
                        size += len(chunk)
                        total += len(chunk)
                        if size > max_member_bytes:
                            raise ArchiveError(f"{name} inside the zip is larger than {_mb(max_member_bytes)}")
                        if total > max_total_bytes:
                            raise ArchiveError(f"the zip unpacks to more than {_mb(max_total_bytes)}")
                        chunks.append(chunk)
            except ArchiveError:
                raise
            except (zipfile.BadZipFile, OSError, EOFError, NotImplementedError) as exc:
                raise ArchiveError(f"{name} inside the zip cannot be read ({exc})") from None
            yield Member(name, b"".join(chunks))


def unpack_zip(
    content: bytes,
    max_member_bytes: int = MAX_TOTAL_BYTES,
    max_files: int = MAX_FILES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> list[Member]:
    """All files at once (small zips and tests); scans use iter_zip."""
    return list(iter_zip(content, max_member_bytes, max_files, max_total_bytes))
