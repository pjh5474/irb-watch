import io
import re
import zlib
from pathlib import Path

import olefile
import pytest

from irb_watch.core.hashing import normalize_text
from irb_watch.parsers import hwp as hwp_module
from irb_watch.parsers.errors import (
    DISTRIBUTION_PROTECTED,
    NO_TEXT_LAYER,
    UNSUPPORTED_FORMAT,
    ParseError,
    TextUnavailableError,
    UnreadableFileError,
)
from irb_watch.parsers.hwp import parse_hwp

FIXTURES = sorted(Path(__file__).parent.joinpath("fixtures").glob("snuh_form_*.hwp"))


# ---- real files (public SNUH forms, committed as fixtures) -----------------------------


@pytest.fixture(params=FIXTURES, ids=lambda p: p.name)
def real_hwp(request):
    return request.param.read_bytes()


def test_real_fixtures_exist():
    assert len(FIXTURES) == 2


def test_reads_real_hwp_forms(real_hwp):
    doc = parse_hwp(real_hwp)
    assert doc.pages == 1 and len(doc.text) > 300
    assert "임상시험용 제제의 치료목적 사용" in doc.text
    assert "IRB No." in doc.text and "Protocol No." in doc.text


def test_text_agrees_with_the_preview_stream_stored_in_the_file(real_hwp):
    """HWP files carry a separate plain-text preview; its words must all be in our text."""
    preview = olefile.OleFileIO(io.BytesIO(real_hwp)).openstream("PrvText").read()
    preview_words = re.findall(r"[\w.]{2,}", preview.decode("utf-16-le", "replace"))
    text = parse_hwp(real_hwp).text
    assert preview_words
    missing = [w for w in preview_words if w not in text]
    assert not missing, missing


def test_real_hwp_hash_is_stable(real_hwp):
    assert parse_hwp(real_hwp).text_hash == parse_hwp(real_hwp).text_hash


def with_header(real: bytes, *, flags: int | None = None, major: int | None = None) -> bytes:
    """Copy of a real file with the FileHeader flags or version rewritten in place."""
    stream = io.BytesIO(real)
    ole = olefile.OleFileIO(stream, write_mode=True)
    header = bytearray(ole.openstream("FileHeader").read())
    if flags is not None:
        header[36:40] = flags.to_bytes(4, "little")
    if major is not None:
        header[35] = major
    ole.write_stream("FileHeader", bytes(header))
    ole.close()
    return stream.getvalue()


def test_password_protected_is_unreadable(real_hwp):
    with pytest.raises(UnreadableFileError, match="password"):
        parse_hwp(with_header(real_hwp, flags=0b11))


@pytest.mark.parametrize("flag", [1 << 4, 1 << 8, 1 << 10])
def test_drm_and_certificate_encryption_are_unreadable(real_hwp, flag):
    with pytest.raises(UnreadableFileError, match="DRM"):
        parse_hwp(with_header(real_hwp, flags=1 | flag))


def test_distribution_document_is_hash_only(real_hwp):
    with pytest.raises(TextUnavailableError) as info:
        parse_hwp(with_header(real_hwp, flags=0b101))
    assert info.value.reason == DISTRIBUTION_PROTECTED


def test_old_hwp_versions_are_unsupported(real_hwp):
    with pytest.raises(TextUnavailableError) as info:
        parse_hwp(with_header(real_hwp, major=3))
    assert info.value.reason == UNSUPPORTED_FORMAT


def test_flag_for_uncompressed_on_compressed_data_is_reported_not_garbled(real_hwp):
    # Wrong flags make the reader treat deflate bytes as records: it must say so, not crash.
    with pytest.raises((UnreadableFileError, TextUnavailableError)):
        parse_hwp(with_header(real_hwp, flags=0))


# ---- broken input ------------------------------------------------------------------------


def test_not_an_ole_container_is_unreadable():
    with pytest.raises(UnreadableFileError, match="corrupt"):
        parse_hwp(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"garbage" * 50)


def test_ole_file_that_is_not_hwp_is_a_mismatch(real_hwp):
    # A real OLE container without a HWP FileHeader (for example an old .doc) is not an HWP.
    stream = io.BytesIO(real_hwp)
    ole = olefile.OleFileIO(stream, write_mode=True)
    header = ole.openstream("FileHeader").read()
    ole.write_stream("FileHeader", b"X" * len(header))
    ole.close()
    with pytest.raises(ParseError, match="wrong signature"):
        parse_hwp(stream.getvalue())


# ---- record-level behaviour (hand-built bytes) ---------------------------------------------


def record(tag: int, payload: bytes, level: int = 0) -> bytes:
    size = len(payload)
    if size >= 0xFFF:
        return ((0xFFF << 20) | (level << 10) | tag).to_bytes(4, "little") + size.to_bytes(
            4, "little"
        ) + payload
    return ((size << 20) | (level << 10) | tag).to_bytes(4, "little") + payload


def units(*parts) -> bytes:
    """Build UTF-16LE paragraph bytes from strings and raw control codes."""
    out = b""
    for part in parts:
        out += part.encode("utf-16-le") if isinstance(part, str) else part.to_bytes(2, "little")
    return out


PARA = 67


def text_of(*records: bytes) -> str:
    return "\n".join(hwp_module._paragraphs(b"".join(records)))


def test_paragraph_text_and_line_breaks():
    got = text_of(record(PARA, units("first line", 10, "second", 13)))
    assert got == "first line\nsecond\n"


def test_other_record_types_are_ignored():
    got = text_of(record(66, b"\x01\x02\x03\x04"), record(PARA, units("kept", 13)))
    assert got.strip() == "kept"


def test_extended_controls_skip_their_fourteen_data_bytes():
    # control 2 (section definition) is followed by 7 more units that are not text
    junk = units(*[0x41] * 7)  # would read as "AAAAAAA" if wrongly treated as text
    got = text_of(record(PARA, units("a", 2) + junk + units("b", 13)))
    assert got.strip() == "ab"


def test_tab_is_kept_and_skips_its_data():
    got = text_of(record(PARA, units("x", 9) + units(*[0x41] * 7) + units("y", 13)))
    assert got.strip() == "x\ty"


def test_hyphen_and_fixed_spaces():
    assert text_of(record(PARA, units("a", 24, "b", 30, "c", 13))).strip() == "a-b c"


def test_characters_outside_the_basic_plane_survive():
    got = text_of(record(PARA, units("hi ") + "\U0001f600".encode("utf-16-le") + units(13)))
    assert "\U0001f600" in got


def test_long_record_uses_the_extended_size_field():
    body = "x" * 3000
    got = text_of(record(PARA, units(body, 13)))
    assert got.strip() == body


def test_truncated_record_is_reported():
    data = record(PARA, units("hello", 13))[:-3]
    with pytest.raises(UnreadableFileError, match="truncated"):
        text_of(data)


def test_odd_trailing_byte_is_ignored():
    assert text_of(record(PARA, units("ok", 13) + b"\x00")).strip() == "ok"


def test_decompression_bomb_is_refused(monkeypatch):
    monkeypatch.setattr(hwp_module, "MAX_UNCOMPRESSED_BYTES", 10_000)
    bomb = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw = bomb.compress(b"\x00" * 1_000_000) + bomb.flush()

    class FakeOle:
        def get_size(self, path):
            return len(raw)

        def openstream(self, path):
            return io.BytesIO(raw)

    with pytest.raises(UnreadableFileError, match="more than the allowed"):
        hwp_module._section_text(FakeOle(), ["BodyText", "Section0"], compressed=True)


def test_corrupt_deflate_data_is_reported():
    class FakeOle:
        def get_size(self, path):
            return 10

        def openstream(self, path):
            return io.BytesIO(b"this is not deflate data")

    with pytest.raises(UnreadableFileError, match="cannot decompress"):
        hwp_module._section_text(FakeOle(), ["BodyText", "Section0"], compressed=True)


def test_hwp_without_text_is_hash_only(real_hwp, monkeypatch):
    monkeypatch.setattr(hwp_module, "_section_text", lambda *a, **k: "  ")
    with pytest.raises(TextUnavailableError) as info:
        parse_hwp(real_hwp)
    assert info.value.reason == NO_TEXT_LAYER


def test_unexpected_bug_is_reported_with_a_note_and_logged(real_hwp, monkeypatch, caplog):
    def boom(*a, **k):
        raise KeyError("surprise")

    monkeypatch.setattr(hwp_module, "_section_text", boom)
    with pytest.raises(UnreadableFileError, match="internal error.*KeyError"):
        parse_hwp(real_hwp)
    assert any("unexpected error while reading HWP" in r.message for r in caplog.records)


def test_normalisation_makes_text_stable_across_whitespace():
    assert normalize_text("a \n b") == normalize_text("a b")
