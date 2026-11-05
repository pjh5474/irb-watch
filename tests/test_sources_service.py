import pytest

from irb_watch.security.url_guard import UnsafeURLError
from irb_watch.services.errors import ConflictError, NotFoundError, ValidationError
from irb_watch.services.sources import SourceService, detect_adapter
from tests.fakes import FakeAudit
from tests.fakes_scan import FakeSourceStore

URL = "https://hrpp.snuh.org/hrpp/bbs/list.do?key=2605277029"


def make_service(checker=lambda url: url):
    audit = FakeAudit()
    return SourceService(FakeSourceStore(), audit, checker), audit


# ---- detect_adapter ------------------------------------------------------------------


def test_detect_snuh_board_and_canonicalise():
    adapter, canonical, config = detect_adapter(
        "http://HRPP.snuh.org/hrpp/bbs/list.do?pageIndex=2&key=2605277029&sw="
    )
    assert adapter == "snuh_hrpp" and config == {"board_key": "2605277029"}
    assert canonical == URL


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/hrpp/bbs/list.do?key=1",
        "https://hrpp.snuh.org/hrpp/bbs/list.do",
        "https://hrpp.snuh.org/hrpp/bbs/list.do?key=abc",
        "https://hrpp.snuh.org/other/page?key=1",
        "not a url",
    ],
)
def test_unsupported_urls_are_rejected(url):
    with pytest.raises(ValidationError):
        detect_adapter(url)


# ---- create / list / update -----------------------------------------------------------


def test_create_logs_and_stores_the_canonical_url():
    service, audit = make_service()
    source = service.create("admin-1", "  Rules ", URL + "&pageIndex=3")
    assert source.name == "Rules" and source.url == URL and source.adapter == "snuh_hrpp"
    assert audit.entries[-1]["event_type"] == "source.created"
    assert audit.entries[-1]["source_id"] == source.id


def test_duplicate_url_conflicts_even_with_different_query_noise():
    service, _ = make_service()
    service.create("a", "Rules", URL)
    with pytest.raises(ConflictError):
        service.create("a", "Again", URL + "&sc=pstTtl")


def test_blank_name_rejected():
    service, _ = make_service()
    with pytest.raises(ValidationError):
        service.create("a", "   ", URL)


def test_url_failing_the_safety_check_is_rejected():
    def refuse(url):
        raise UnsafeURLError("host resolves to non-public address")

    service, audit = make_service(refuse)
    with pytest.raises(ValidationError, match="not allowed"):
        service.create("a", "Rules", URL)
    assert audit.entries == []


def test_update_name_and_active_flag():
    service, audit = make_service()
    source = service.create("a", "Rules", URL)
    updated = service.update("a", source.id, name=" Renamed ", is_active=False)
    assert updated.name == "Renamed" and updated.is_active is False
    assert audit.entries[-1]["event_type"] == "source.updated"


def test_update_with_nothing_to_change_is_a_no_op():
    service, audit = make_service()
    source = service.create("a", "Rules", URL)
    count = len(audit.entries)
    assert service.update("a", source.id).name == "Rules"
    assert len(audit.entries) == count


def test_update_unknown_source():
    service, _ = make_service()
    with pytest.raises(NotFoundError):
        service.update("a", 99, name="x")
