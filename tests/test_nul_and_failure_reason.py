from datetime import UTC, datetime

from irb_watch.core.hashing import collapse_whitespace, normalize_text
from irb_watch.services.scan import ScanService
from tests.fakes import FakeAudit
from tests.fakes_scan import FakeFileStorage, FakeReader, FakeScanRepo, make_source

NUL = chr(0)


def test_nul_characters_are_removed_from_text():
    assert collapse_whitespace(f"a{NUL}b  c") == "ab c"
    zero_width = chr(0x200B)
    assert normalize_text(f"{NUL}한{NUL}글 {zero_width} text") == "한글 text"


def test_unexpected_error_shows_its_reason():
    repo = FakeScanRepo()
    repo.add_source(make_source(1))
    reader = FakeReader(error=RuntimeError("unsupported Unicode escape sequence\nsecond line"))
    service = ScanService(
        repo,
        FakeFileStorage(),
        lambda source: reader,
        FakeAudit(),
        clock=lambda: datetime(2026, 11, 6, tzinfo=UTC),
    )
    result = service.scan_source(1, "admin-1")
    assert result.status == "failed"
    assert result.error == "unexpected error (RuntimeError): unsupported Unicode escape sequence"
