import unicodedata

import pytest

from irb_watch.core.hashing import normalize_text
from irb_watch.parsers import pdf as pdf_module
from irb_watch.parsers.errors import (
    NO_TEXT_LAYER,
    UNSUPPORTED_FORMAT,
    ParseError,
    TextUnavailableError,
    UnsupportedFormatError,
)
from irb_watch.parsers.formats import detect_format, extract_text, parse_document
from irb_watch.parsers.pdf import parse_pdf

LONG = "Clinical trial guideline revised in August for all sites"


def make_pdf(pages: list[str]) -> bytes:
    """Build a tiny valid text PDF (Helvetica, ASCII only). An empty string makes a blank page."""
    n = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(pages):
        bodies.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> >> >>".encode()
        )
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
        bodies.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(bodies) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(bodies) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


# ---- parse_pdf -----------------------------------------------------------------


def test_extracts_text_from_all_pages():
    doc = parse_pdf(make_pdf([LONG, "Second page text goes here"]))
    assert LONG in doc.text and "Second page text goes here" in doc.text
    assert doc.pages == 2 and doc.empty_pages == 0
    assert len(doc.text_hash) == 64


def test_same_content_gives_same_hash():
    assert parse_pdf(make_pdf([LONG])).text_hash == parse_pdf(make_pdf([LONG])).text_hash


def test_different_content_gives_different_hash():
    other = LONG.replace("August", "September")
    assert parse_pdf(make_pdf([LONG])).text_hash != parse_pdf(make_pdf([other])).text_hash


def test_blank_page_is_counted_not_fatal():
    doc = parse_pdf(make_pdf([LONG, ""]))
    assert doc.pages == 2 and doc.empty_pages == 1


def test_pdf_without_text_layer_is_hash_only_not_an_empty_document():
    with pytest.raises(TextUnavailableError, match="no extractable text") as info:
        parse_pdf(make_pdf(["", ""]))
    assert info.value.reason == NO_TEXT_LAYER


def test_very_short_text_counts_as_no_text():
    with pytest.raises(TextUnavailableError):
        parse_pdf(make_pdf(["Page 1"]))


def test_text_unavailable_is_not_a_parse_error():
    # `except ParseError` (the needs_review path) must never catch the expected hash-only case.
    assert not issubclass(TextUnavailableError, ParseError)
    assert not issubclass(UnsupportedFormatError, ParseError)
    with pytest.raises(TextUnavailableError):
        try:
            parse_pdf(make_pdf(["", ""]))
        except ParseError:
            pytest.fail("scanned PDF was reported as a failure")


def test_not_a_pdf():
    with pytest.raises(ParseError, match="not a PDF"):
        parse_pdf(b"<html>login required</html>")


def test_corrupt_pdf():
    with pytest.raises(ParseError, match="could not read PDF"):
        parse_pdf(b"%PDF-1.4\nthis is not really a pdf at all")


def test_empty_bytes():
    with pytest.raises(ParseError):
        parse_pdf(b"")


def test_too_many_pages():
    with pytest.raises(ParseError, match="limit"):
        parse_pdf(make_pdf([LONG] * 3), max_pages=2)


def test_password_protected(monkeypatch):
    def locked(_):
        raise pdf_module.PDFPasswordIncorrect

    monkeypatch.setattr(pdf_module.pdfplumber, "open", locked)
    with pytest.raises(ParseError, match="password-protected"):
        parse_pdf(b"%PDF-1.4 anything")


# ---- normalize_text -------------------------------------------------------------
# Special characters are built from code points so no invisible characters sit in this file.

NBSP = chr(0x00A0)
ZERO_WIDTH_SPACE = chr(0x200B)
BOM = chr(0xFEFF)


def test_normalize_collapses_whitespace_and_invisible_characters():
    messy = f"  a {NBSP}\n\t b{ZERO_WIDTH_SPACE} c{BOM} "
    assert normalize_text(messy) == "a b c"


def test_normalize_unifies_composed_and_decomposed_korean():
    composed = unicodedata.normalize("NFC", "한글")
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    assert normalize_text(composed) == normalize_text(decomposed) == composed


# ---- format detection -----------------------------------------------------------


def test_detect_formats():
    assert detect_format("a.pdf", b"%PDF-1.7 ...") == "pdf"
    assert detect_format("misnamed.txt", b"%PDF-1.7 ...") == "pdf"
    assert detect_format("a.hwp", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest") == "hwp"
    assert detect_format("a.hwpx", b"PK\x03\x04rest") == "hwpx"
    assert detect_format("a.docx", b"PK\x03\x04rest") == "docx"
    assert detect_format("a.pdf", b"<html>") == "unknown"


def test_parse_document_uses_pdf_parser():
    assert LONG in parse_document("x.pdf", make_pdf([LONG])).text


def test_parse_document_reports_unsupported_types():
    with pytest.raises(UnsupportedFormatError, match="hwp"):
        parse_document("form.hwp", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest")


# ---- extract_text: the pipeline entry point ----------------------------------------


def test_extract_text_returns_document_for_text_pdf():
    result = extract_text("a.pdf", make_pdf([LONG]))
    assert result.has_text and result.hash_only_reason is None
    assert LONG in result.document.text


def test_extract_text_marks_scanned_pdf_hash_only():
    result = extract_text("scan.pdf", make_pdf(["", ""]))
    assert not result.has_text and result.hash_only_reason == NO_TEXT_LAYER


def test_extract_text_marks_unsupported_format_hash_only():
    result = extract_text("form.hwp", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest")
    assert not result.has_text and result.hash_only_reason == UNSUPPORTED_FORMAT


def test_unknown_extension_is_hash_only_but_wrong_content_for_known_extension_fails():
    assert extract_text("archive.zip", b"PK\x03\x04data").hash_only_reason == UNSUPPORTED_FORMAT
    with pytest.raises(ParseError, match="not a valid hwp"):
        extract_text("form.hwp", b"<html>login</html>")
    with pytest.raises(ParseError, match="not a valid docx"):
        extract_text("form.docx", b"%PDF-1.4 hmm")


def test_extract_text_still_raises_for_real_failures():
    with pytest.raises(ParseError):
        extract_text("broken.pdf", b"%PDF-1.4\nnot really a pdf")
    with pytest.raises(ParseError):
        extract_text("page.pdf", b"<html>login</html>")
