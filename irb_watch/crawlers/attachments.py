"""Turn a post's attachments into the file records of a version. Shared by every reader.

A normal file -> one record (text read, or tracked by hash with a reason).
A .zip -> the zip itself (kept, downloadable) plus one record per file inside, named
"<zip name>/<path inside>", each stored and downloadable on its own with its text read.
A zip that cannot be unpacked within the limits stays one record with the reason, and the
post gets a warning (see parsers/archive.py).

Every file is checked BEFORE it is stored: a download that is not the file it claims to be
(an HTML error page named .pdf) fails the post without leaving anything in storage.
"""

from collections.abc import Callable, Iterable

from irb_watch.core.hashing import sha256_hex
from irb_watch.core.models import AttachmentRef, DownloadedFile
from irb_watch.core.state import TEXT_IMAGE_NOTE, FileContent
from irb_watch.crawlers.errors import (
    FetchError,
    FileTooLargeError,
    RobotsDisallowedError,
    SiteUnavailableError,
    TransientFetchError,
)
from irb_watch.parsers.archive import ArchiveError, is_zip, iter_zip
from irb_watch.parsers.errors import (
    INLINE_IMAGE,
    TOO_LARGE,
    UNREADABLE,
    UNSUPPORTED_FORMAT,
    ParseError,
)
from irb_watch.parsers.formats import extract_text

# file_sink(sha256, filename, content) stores the original and returns its storage path.
FileSink = Callable[[str, str, bytes], str]


def size_label(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}MB"
    if n >= 1_000:
        return f"{n / 1_000:.0f}KB"
    return f"{n}B"


def plain_note(note: str | None) -> str | None:
    """A file note never contains ";": a post's warnings are joined with "; ", and keeping
    that separator out of the notes lets them be split back exactly (services/reviews.py)."""
    return note.replace(";", ",") if note else note


def _record(name: str, label: str, sha: str, content: bytes, file_sink: FileSink) -> FileContent:
    """Read first, store second. Raises ParseError (nothing stored) when the bytes are not
    what the name promises."""
    extracted = extract_text(name, content)
    return FileContent(
        filename=name,
        size_label=label,
        sha256=sha,
        text=extracted.document.text if extracted.document else None,
        hash_only_reason=extracted.hash_only_reason,
        storage_path=file_sink(sha, name, content),
        note=plain_note(extracted.note),
    )


def read_attachment(
    name: str, label: str, content: bytes, sha: str, file_sink: FileSink, max_member_bytes: int
) -> tuple[list[FileContent], list[str]]:
    """Return (file records, warnings). A ParseError on the attachment itself propagates: it
    means the download was not the file (for example an HTML error page), so the post fails
    and keeps its old state. Files inside a zip are capped at max_member_bytes, like a
    download."""
    if not is_zip(name, content):
        record = _record(name, label, sha, content, file_sink)
        warnings = [f"{name}: {record.note}"] if record.hash_only_reason == UNREADABLE else []
        return [record], warnings

    inner_records: list[FileContent] = []
    warnings: list[str] = []
    try:
        for member in iter_zip(content, max_member_bytes):
            inner = f"{name}/{member.name}"
            inner_sha = sha256_hex(member.content)
            inner_label = size_label(len(member.content))
            try:
                record = _record(inner, inner_label, inner_sha, member.content, file_sink)
            except ParseError as exc:
                # Inside a zip this is a property of the archive's content, not a download
                # error: keep the file (downloadable) and flag it.
                # The parser's message starts with the file name; the warning adds it once.
                reason = plain_note(str(exc).removeprefix(f"{inner}: "))
                record = FileContent(
                    inner, inner_label, inner_sha, None, UNREADABLE,
                    file_sink(inner_sha, inner, member.content), note=reason,
                )
            if record.hash_only_reason == UNREADABLE:
                warnings.append(f"{inner}: {record.note}")
            inner_records.append(record)
    except ArchiveError as exc:
        # Files already stored stay in storage (hash-named, harmless); the version lists only
        # the zip, with the reason.
        note = plain_note(str(exc))
        return (
            [FileContent(name, label, sha, None, UNREADABLE, file_sink(sha, name, content), note=note)],
            [f"{name}: {note}"],
        )

    original = FileContent(
        name, label, sha, None, UNSUPPORTED_FORMAT, file_sink(sha, name, content),
        note=f"zip archive with {len(inner_records)} files, each listed separately",
    )
    return [original, *inner_records], warnings


def too_large_record(
    name: str, label: str, max_file_bytes: int, stable_id: str | None = None
) -> FileContent:
    """An attachment over the size limit, listed but not downloaded. Nothing is stored for it.

    Its stand-in hash comes from what the site tells about the file without downloading it:
    the name, the size shown, and the site's own id for the file when it has one (R-BAY's
    download key; a replaced file gets a new key even at the same size). The same big file is
    then "unchanged" on the next scan and a replacement counts as a change. Known limit: if
    MAX_FILE_BYTES is later raised above the file's size, the next scan downloads it and its
    real hash replaces the stand-in, which shows once as a replaced file."""
    limit = f"{max_file_bytes / 1_000_000:.0f} MB"
    shown = f" (the site says {label})" if label else ""
    return FileContent(
        filename=name,
        size_label=label,
        sha256=sha256_hex(f"too-large\0{name}\0{label}\0{stable_id or ''}".encode()),
        text=None,
        hash_only_reason=TOO_LARGE,
        storage_path=None,
        note=f"not downloaded: larger than {limit}{shown}, open it on the hospital site",
    )


_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


def is_image(content: bytes) -> bool:
    """PNG, JPEG, GIF or WebP, by their first bytes (an HTML error page is not one)."""
    return content.startswith(_IMAGE_MAGIC) or (content[:4] == b"RIFF" and content[8:12] == b"WEBP")


def _text_image(
    name: str,
    ref: AttachmentRef,
    download: Callable[[AttachmentRef, int], DownloadedFile],
    max_file_bytes: int,
    file_sink: FileSink,
) -> tuple[FileContent, str | None]:
    """An image of the post's text -> (record, warning or None). Stored without text (reason
    inline_image, never a warning). A permanent problem (the site answers an error, or the bytes
    are not an image) keeps the post with a placeholder whose hash depends only on the address,
    so the same broken image is "unchanged" next time, plus a warning an admin can mark OK.
    Temporary errors (timeouts, the site down, an ended session) are not caught: the post then
    fails and keeps its old state, as for attachments, so a blip never looks like a change."""
    try:
        downloaded = download(ref, max_file_bytes)
    except FileTooLargeError:
        record = too_large_record(name, ref.size_label, max_file_bytes, ref.stable_id)
        return record, f"{name}: {record.note}"
    except (TransientFetchError, SiteUnavailableError):
        raise
    except (FetchError, RobotsDisallowedError) as exc:
        return _image_placeholder(name, ref, f"could not be loaded ({plain_note(str(exc))[:160]})")
    if not is_image(downloaded.content):
        return _image_placeholder(name, ref, "the site sent something that is not an image")
    sha = downloaded.sha256
    return FileContent(
        filename=name,
        size_label=size_label(len(downloaded.content)),
        sha256=sha,
        text=None,
        hash_only_reason=INLINE_IMAGE,
        storage_path=file_sink(sha, name, downloaded.content),
        note=TEXT_IMAGE_NOTE,
    ), None


def _image_placeholder(name: str, ref: AttachmentRef, why: str) -> tuple[FileContent, str]:
    note = f"{TEXT_IMAGE_NOTE}: {why}, open the post on the hospital site"
    record = FileContent(
        filename=name,
        size_label="",
        sha256=sha256_hex(f"text-image-unavailable\0{ref.stable_id or ref.url}".encode()),
        text=None,
        hash_only_reason=UNREADABLE,
        storage_path=None,
        note=note,
    )
    return record, f"{name}: {note}"


def observe_files(
    refs: Iterable[AttachmentRef],
    download: Callable[[AttachmentRef, int], DownloadedFile],
    max_file_bytes: int,
    file_sink: FileSink,
) -> tuple[list[FileContent], list[str]]:
    """Download and read every attachment of one post. Two files with the same name get
    " (2)", " (3)" so each stays its own record."""
    files: list[FileContent] = []
    warnings: list[str] = []
    seen: dict[str, int] = {}
    for ref in refs:
        count = seen.get(ref.filename, 0) + 1
        seen[ref.filename] = count
        name = ref.filename if count == 1 else f"{ref.filename} ({count})"
        if ref.inline_image:
            record, problem = _text_image(name, ref, download, max_file_bytes, file_sink)
            files.append(record)
            warnings.extend([problem] if problem else [])
            continue
        try:
            downloaded = download(ref, max_file_bytes)
        except FileTooLargeError:
            # Only the file body can say "too large" here: adapters turn an oversized answer
            # to any other request (a check, a login page) into a plain FetchError, which fails
            # the post with its reason. Only this file is skipped; the post is kept.
            record = too_large_record(name, ref.size_label, max_file_bytes, ref.stable_id)
            files.append(record)
            warnings.append(f"{name}: {record.note}")
            continue
        records, problems = read_attachment(
            name, ref.size_label, downloaded.content, downloaded.sha256, file_sink, max_file_bytes
        )
        files.extend(records)
        warnings.extend(problems)
    return files, warnings
