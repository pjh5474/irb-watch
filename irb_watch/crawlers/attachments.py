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
from irb_watch.core.state import FileContent
from irb_watch.parsers.archive import ArchiveError, is_zip, iter_zip
from irb_watch.parsers.errors import UNREADABLE, UNSUPPORTED_FORMAT, ParseError
from irb_watch.parsers.formats import extract_text

# file_sink(sha256, filename, content) stores the original and returns its storage path.
FileSink = Callable[[str, str, bytes], str]


def size_label(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}MB"
    if n >= 1_000:
        return f"{n / 1_000:.0f}KB"
    return f"{n}B"


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
        note=extracted.note,
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
                reason = str(exc).removeprefix(f"{inner}: ")
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
        return (
            [FileContent(name, label, sha, None, UNREADABLE, file_sink(sha, name, content), note=str(exc))],
            [f"{name}: {exc}"],
        )

    original = FileContent(
        name, label, sha, None, UNSUPPORTED_FORMAT, file_sink(sha, name, content),
        note=f"zip archive with {len(inner_records)} files, each listed separately",
    )
    return [original, *inner_records], warnings


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
        downloaded = download(ref, max_file_bytes)
        count = seen.get(ref.filename, 0) + 1
        seen[ref.filename] = count
        name = ref.filename if count == 1 else f"{ref.filename} ({count})"
        records, problems = read_attachment(
            name, ref.size_label, downloaded.content, downloaded.sha256, file_sink, max_file_bytes
        )
        files.extend(records)
        warnings.extend(problems)
    return files, warnings
