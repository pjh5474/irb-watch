"""Zip attachments: unpacked in memory with limits; every file inside becomes its own record."""

import io
import zipfile

import pytest

from irb_watch.crawlers.attachments import read_attachment, size_label
from irb_watch.parsers.archive import ArchiveError, is_zip, iter_zip, unpack_zip
from irb_watch.parsers.errors import UNREADABLE, UNSUPPORTED_FORMAT
from irb_watch.parsers.formats import ParseError


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buffer.getvalue()


def cp949_zip(name: str, content: bytes) -> bytes:
    """What Korean Windows tools write: the name as CP949 bytes and no UTF-8 flag. Python's
    writer would store UTF-8, so write an ASCII stand-in of the same length and swap the bytes."""
    raw = name.encode("cp949")
    stand_in = b"A" * len(raw)
    data = make_zip({stand_in.decode(): content})
    assert data.count(stand_in) == 2  # local header and central directory
    return data.replace(stand_in, raw)


def test_is_zip_needs_the_name_and_the_bytes():
    data = make_zip({"a.txt": b"x"})
    assert is_zip("IRB Submission.zip", data)
    assert not is_zip("form.docx", data)  # a DOCX is a zip inside, but not an archive to unpack
    assert not is_zip("fake.zip", b"<html>")


def test_unpack_keeps_folders_and_skips_mac_junk():
    data = make_zip({"docs/protocol.txt": b"p", "__MACOSX/docs/._protocol.txt": b"j", "Thumbs.db": b"t"})
    assert [(m.name, m.content) for m in unpack_zip(data)] == [("docs/protocol.txt", b"p")]


def test_office_lock_files_are_skipped():
    # A real R-BAY zip held Word's hidden lock file next to the document (2026-11-10).
    data = make_zip({"서식/~$ 대상자 설명서 및 동의서.docx": b"\x05user", "서식/대상자 설명서 및 동의서.docx": b"doc"})
    assert [m.name for m in unpack_zip(data)] == ["서식/대상자 설명서 및 동의서.docx"]


def test_korean_names_written_by_windows_tools_are_read_correctly():
    data = cp949_zip("연구계획서.txt", b"x")
    assert unpack_zip(data)[0].name == "연구계획서.txt"


def test_path_tricks_in_names_are_cleaned():
    data = make_zip({"../../etc/passwd": b"x"})
    assert unpack_zip(data)[0].name == "etc/passwd"


def test_limits_on_file_count_and_unpacked_size():
    many = make_zip({f"f{i}.txt": b"x" for i in range(4)})
    with pytest.raises(ArchiveError, match="4 files"):
        unpack_zip(many, max_files=3)
    big = make_zip({"a.txt": b"0" * 5000, "b.txt": b"0" * 5000})  # compresses to almost nothing
    with pytest.raises(ArchiveError, match="more than"):
        unpack_zip(big, max_total_bytes=8000)


def test_a_file_inside_a_zip_obeys_the_normal_download_cap():
    data = make_zip({"small.txt": b"x", "huge.pdf": b"0" * 5000})
    with pytest.raises(ArchiveError, match="huge.pdf inside the zip is larger"):
        unpack_zip(data, max_member_bytes=4000)


def test_a_lying_size_in_the_zip_is_caught_while_reading():
    data = bytearray(make_zip({"a.txt": b"0" * 5000}))
    # Make both headers claim 10 bytes; the real content is still 5000.
    for marker, offset in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
        at = data.find(marker) + offset
        data[at : at + 4] = (10).to_bytes(4, "little")
    with pytest.raises(ArchiveError):
        unpack_zip(bytes(data), max_member_bytes=4000)


def test_zip_files_are_read_one_at_a_time():
    data = make_zip({"a.txt": b"1", "b.txt": b"2"})
    stream = iter_zip(data, max_member_bytes=100)
    assert next(stream).name == "a.txt"  # a generator: the second file is not unpacked yet
    assert next(stream).name == "b.txt"


def test_damaged_and_encrypted_zips_say_why():
    with pytest.raises(ArchiveError, match="damaged"):
        unpack_zip(b"PK\x03\x04 not really a zip")
    data = bytearray(make_zip({"a.txt": b"x"}))
    # Set the "encrypted" flag in the central directory entry.
    central = data.rfind(b"PK\x01\x02")
    data[central + 8] |= 0x1
    with pytest.raises(ArchiveError, match="password"):
        unpack_zip(bytes(data))


def sink(stored):
    return lambda sha, name, content: stored.append(name) or f"path/{sha}"


def test_a_zip_becomes_itself_plus_each_file_inside():
    data = make_zip({"a.txt": b"hello", "b.pdf": b"not a pdf"})
    stored = []
    records, warnings = read_attachment("pack.zip", "1KB", data, "z" * 64, sink(stored), 10_000)
    assert [r.filename for r in records] == ["pack.zip", "pack.zip/a.txt", "pack.zip/b.pdf"]
    assert sorted(stored) == ["pack.zip", "pack.zip/a.txt", "pack.zip/b.pdf"]  # each downloadable
    original, text_file, broken = records
    assert original.hash_only_reason == UNSUPPORTED_FORMAT and "2 files" in original.note
    assert text_file.hash_only_reason == UNSUPPORTED_FORMAT and text_file.size_label == "5B"
    # A broken file inside a zip does not fail the post; it is flagged with the reason.
    assert broken.hash_only_reason == UNREADABLE and warnings == [f"pack.zip/b.pdf: {broken.note}"]
    assert warnings[0].count("pack.zip/b.pdf") == 1  # the name is said once, not twice
    assert len({r.sha256 for r in records}) == 3


def test_file_notes_never_contain_the_warning_separator():
    # "the zip is password-protected; its files cannot be read" -> no ";" in the stored note,
    # so a post's warnings ("a; b") can be split back exactly.
    data = bytearray(make_zip({"a.txt": b"x"}))
    central = data.rfind(b"PK\x01\x02")
    data[central + 8] |= 0x1
    records, warnings = read_attachment("pack.zip", "1KB", bytes(data), "z" * 64, sink([]), 10_000)
    assert ";" not in records[0].note and ";" not in warnings[0]
    assert "password-protected, its files cannot be read" in records[0].note


def test_a_zip_that_cannot_be_unpacked_is_kept_and_flagged():
    records, warnings = read_attachment("pack.zip", "1KB", b"PK\x03\x04 broken", "z" * 64, sink([]), 10_000)
    assert len(records) == 1 and records[0].hash_only_reason == UNREADABLE
    assert "damaged" in warnings[0]


def test_a_normal_file_that_is_really_an_error_page_fails_the_post_and_is_not_stored():
    stored = []
    with pytest.raises(ParseError):
        read_attachment("a.pdf", "1KB", b"<html>login</html>", "a" * 64, sink(stored), 10_000)
    assert stored == []  # checked before storing: nothing left behind


def test_size_labels():
    assert (size_label(5), size_label(2_500), size_label(7_380_000)) == ("5B", "2KB", "7.38MB")
