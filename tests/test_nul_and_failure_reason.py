from irb_watch.core.hashing import collapse_whitespace, normalize_text
from tests.fakes_scan import scan_once

NUL = chr(0)


def test_nul_characters_are_removed_from_text():
    assert collapse_whitespace(f"a{NUL}b  c") == "ab c"
    zero_width = chr(0x200B)
    assert normalize_text(f"{NUL}한{NUL}글 {zero_width} text") == "한글 text"


def test_unexpected_error_shows_its_reason():
    result, _ = scan_once(RuntimeError("unsupported Unicode escape sequence\nsecond line"))
    assert result.status == "failed"
    assert result.error == "unexpected error (RuntimeError): unsupported Unicode escape sequence"
