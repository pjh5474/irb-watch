import io
import zipfile

import pytest

from irb_watch.parsers import _zip
from irb_watch.parsers import docx as docx_module
from irb_watch.parsers.docx import parse_docx
from irb_watch.parsers.errors import (
    NO_TEXT_LAYER,
    ParseError,
    TextUnavailableError,
    UnreadableFileError,
)
from irb_watch.parsers.hwpx import parse_hwpx

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
MC = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'


def make_zip(parts: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return buf.getvalue()


def docx(body: str, **extra: str) -> bytes:
    parts = {
        "[Content_Types].xml": "<Types/>",
        "word/document.xml": f"<w:document {W} {MC}><w:body>{body}</w:body></w:document>",
    }
    for name, xml in extra.items():
        parts[f"word/{name}.xml"] = f"<w:root {W}>{xml}</w:root>"
    return make_zip(parts)


def p(*runs: str) -> str:
    return "<w:p>" + "".join(runs) + "</w:p>"


def r(text: str) -> str:
    return f"<w:r><w:t>{text}</w:t></w:r>"


LONG = "Clinical trial guideline revised for all participating sites"


# ---- DOCX ----------------------------------------------------------------------------------


def test_docx_body_and_table_text():
    table = "<w:tbl><w:tr><w:tc>" + p(r("cell one")) + "</w:tc><w:tc>" + p(r("cell two")) + "</w:tc></w:tr></w:tbl>"
    doc = parse_docx(docx(p(r(LONG)) + table))
    assert LONG in doc.text and "cell one" in doc.text and "cell two" in doc.text
    assert doc.text.index(LONG) < doc.text.index("cell one")


def test_docx_tracked_changes_keep_inserted_and_drop_deleted():
    body = p(
        r("Start of the sentence "),
        "<w:del><w:r><w:delText>OLD WORDS</w:delText></w:r></w:del>",
        "<w:ins>" + r("NEW WORDS") + "</w:ins>",
        r(" and the end of it"),
    )
    text = parse_docx(docx(body)).text
    assert "NEW WORDS" in text and "OLD WORDS" not in text


def test_docx_moved_text_is_kept_once():
    body = p(
        "<w:moveFrom><w:r><w:t>moved away text here</w:t></w:r></w:moveFrom>",
        "<w:moveTo>" + r("moved to text here") + "</w:moveTo>",
        r(" plus enough other words to pass the minimum"),
    )
    text = parse_docx(docx(body)).text
    assert "moved to text here" in text and "moved away" not in text


def test_docx_headers_footers_footnotes_are_included_comments_are_not():
    doc = parse_docx(
        docx(
            p(r(LONG)),
            header1=p(r("Version 3.2 header")),
            footer1=p(r("Page footer text")),
            footnotes=p(r("a footnote")),
            endnotes=p(r("an endnote")),
            comments=p(r("reviewer comment")),
        )
    )
    for expected in ("Version 3.2 header", "Page footer text", "a footnote", "an endnote"):
        assert expected in doc.text
    assert "reviewer comment" not in doc.text
    assert doc.pages == 5


def test_docx_text_box_fallback_is_not_counted_twice():
    box = (
        "<mc:AlternateContent>"
        "<mc:Choice><w:txbxContent>" + p(r("boxed text once")) + "</w:txbxContent></mc:Choice>"
        "<mc:Fallback><w:txbxContent>" + p(r("boxed text once")) + "</w:txbxContent></mc:Fallback>"
        "</mc:AlternateContent>"
    )
    text = parse_docx(docx(p(r(LONG)) + box)).text
    assert text.count("boxed text once") == 1


def test_docx_tabs_breaks_and_field_codes():
    body = p(
        r("before"),
        "<w:r><w:tab/></w:r>",
        "<w:r><w:br/></w:r>",
        "<w:r><w:instrText> HYPERLINK 'x' </w:instrText></w:r>",
        r("after that comes a long enough sentence"),
    )
    text = parse_docx(docx(body)).text
    assert "before" in text and "after that" in text and "HYPERLINK" not in text


def test_docx_text_hash_ignores_formatting_only_changes():
    plain = parse_docx(docx(p(r(LONG))))
    split = parse_docx(docx(p(r(LONG[:20]), r(LONG[20:]))))
    assert plain.text_hash == split.text_hash


def test_docx_without_text_is_hash_only():
    with pytest.raises(TextUnavailableError) as info:
        parse_docx(docx(p(r("hi"))))
    assert info.value.reason == NO_TEXT_LAYER


def test_docx_missing_body_is_a_mismatch():
    with pytest.raises(ParseError, match="word/document.xml"):
        parse_docx(make_zip({"xl/workbook.xml": "<x/>"}))


def test_docx_corrupt_zip_is_unreadable():
    with pytest.raises(UnreadableFileError, match="corrupt"):
        parse_docx(b"PK\x03\x04 this is not a zip")


def test_docx_malformed_xml_is_unreadable():
    broken = make_zip({"word/document.xml": "<w:document><unclosed>"})
    with pytest.raises(UnreadableFileError, match="malformed or unsafe XML"):
        parse_docx(broken)


def test_docx_entity_bomb_is_refused_not_expanded():
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        f"<w:document {W}><w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>"
    )
    with pytest.raises(UnreadableFileError, match="unsafe XML"):
        parse_docx(make_zip({"word/document.xml": bomb}))


def test_docx_zip_bomb_is_refused(monkeypatch):
    monkeypatch.setattr(_zip, "MAX_UNCOMPRESSED_BYTES", 1_000)
    with pytest.raises(UnreadableFileError, match="more than the allowed"):
        parse_docx(docx(p(r("x" * 5_000))))


def test_docx_password_protected_zip_entry():
    data = bytearray(make_zip({"word/document.xml": "<x/>"}))
    # zipfile clears the "encrypted" bit when writing, so set it in the raw headers:
    # general-purpose flags sit at offset 6 of a local header and offset 8 of a central one.
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        data[data.index(signature) + offset] |= 0x1
    with pytest.raises(UnreadableFileError, match="password"):
        parse_docx(bytes(data))


def test_docx_encrypted_office_container(monkeypatch):
    class FakeOle:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def exists(self, name):
            return name == "EncryptedPackage"

    monkeypatch.setattr(docx_module.olefile, "OleFileIO", lambda data: FakeOle())
    with pytest.raises(UnreadableFileError, match="password"):
        parse_docx(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest")


def test_docx_that_is_an_old_binary_word_file(monkeypatch):
    class FakeOle:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def exists(self, name):
            return False

    monkeypatch.setattr(docx_module.olefile, "OleFileIO", lambda data: FakeOle())
    with pytest.raises(ParseError, match="old binary Word"):
        parse_docx(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest")


# ---- HWPX ------------------------------------------------------------------------------------

HP = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'


def hwpx(sections: dict[int, str], **extra: str) -> bytes:
    parts: dict[str, str | bytes] = {"mimetype": "application/hwp+zip"}
    for number, body in sections.items():
        parts[f"Contents/section{number}.xml"] = f"<hp:sec {HP}>{body}</hp:sec>"
    parts.update(extra)
    return make_zip(parts)


def hp_p(text: str) -> str:
    return f"<hp:p><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>"


def test_hwpx_text_in_section_order():
    doc = parse_hwpx(hwpx({10: hp_p("tenth section text goes here"), 2: hp_p(LONG)}))
    assert doc.text.index(LONG) < doc.text.index("tenth section")
    assert doc.pages == 2


def test_hwpx_tabs_and_line_breaks():
    body = "<hp:p><hp:run><hp:t>one<hp:tab/>two</hp:t></hp:run></hp:p>" + hp_p(LONG)
    assert "one" in parse_hwpx(hwpx({0: body})).text


def test_hwpx_without_sections_is_a_mismatch():
    with pytest.raises(ParseError, match="no Contents/section"):
        parse_hwpx(make_zip({"mimetype": "application/hwp+zip"}))


def test_hwpx_encrypted_is_unreadable():
    data = hwpx({0: hp_p(LONG)}, **{"META-INF/manifest.xml": "<m><encryption-data/></m>"})
    with pytest.raises(UnreadableFileError, match="password"):
        parse_hwpx(data)


def test_hwpx_corrupt_zip():
    with pytest.raises(UnreadableFileError):
        parse_hwpx(b"PK\x03\x04 nope")


def test_hwpx_without_text_is_hash_only():
    with pytest.raises(TextUnavailableError):
        parse_hwpx(hwpx({0: hp_p("hi")}))
