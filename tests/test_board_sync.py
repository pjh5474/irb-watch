"""Board sync: add the known boards an R-BAY hospital has (services/board_sync.py)."""

import pytest

from irb_watch.services.board_sync import BoardSyncService
from irb_watch.services.errors import NotFoundError, ValidationError
from irb_watch.services.sources import SourceService
from tests.fakes import FakeAudit
from tests.fakes_scan import FakeSourceStore, fake_hospitals

CODE = "ZDZxdUxtWlZqRTYwWjRocTUwRWY3UT09"
ADDRESS = f"https://www.r-bay.co.kr/agency/main/{CODE}"
FULL_MENU = {
    "IRB_LIST": "IRB위원 리스트", "PHARM_LIST": "관리약사 명단", "REGLA": "관련규정", "LAWS": "관련법규",
    "FORM_BOX": "양식함", "ETC": "기타", "NOTI": "공지", "NEWS": "뉴스", "FAQ": "FAQ", "QnA": "Q&A",
}


def make(menu=FULL_MENU, boards=("NOTI", "FORM_BOX", "REGLA")):
    hospitals = fake_hospitals()
    store = FakeSourceStore()
    sources = SourceService(store, FakeAudit(), lambda url: url, hospitals=hospitals)
    _, hospital_id, _ = sources.create_rbay_hospital("a", ADDRESS, "Gachon Hospital", "owner", list(boards))
    calls = []

    def read_menu(user, agency, site):
        calls.append((user, agency, site))
        if isinstance(menu, Exception):
            raise menu
        return menu

    return BoardSyncService(sources, hospitals, read_menu), sources, hospital_id, calls


def test_dry_run_lists_the_missing_known_boards_and_creates_nothing():
    sync, sources, hid, calls = make()
    result = sync.sync("a", hid)  # dry run is the default
    g = result.groups[0]
    assert result.dry_run and g.error is None
    assert g.already == ["REGLA", "FORM_BOX", "NOTI"]
    assert g.to_create == ["IRB_LIST", "NEWS", "ETC", "FAQ"]
    assert g.unknown == ["PHARM_LIST", "LAWS", "QnA"]  # in the menu, not read by the server
    assert g.created == [] and len(sources.list()) == 3
    assert calls == [("owner", CODE, "rbay-" + CODE)]  # the hospital's own login site


def test_apply_makes_the_sources_with_the_right_kind_name_login_and_ai_on():
    sync, sources, hid, _ = make()
    g = sync.sync("a", hid, dry_run=False).groups[0]
    made = {s.config["board"]: s for s in g.created}
    assert set(made) == {"IRB_LIST", "NEWS", "ETC", "FAQ"} and g.failed == {}
    assert made["IRB_LIST"].board_kind == "committee_members" and made["NEWS"].board_kind == "news"
    assert made["FAQ"].board_kind == "faq" and made["ETC"].board_kind == "other"
    assert made["IRB_LIST"].name == "Gachon Hospital : IRB위원 리스트"
    assert all(s.config["login_user"] == "owner" and s.requires_login and s.ai_enabled for s in g.created)
    assert all(s.hospital_id == hid for s in g.created) and len(sources.list()) == 7


def test_a_second_run_finds_nothing_to_make():
    sync, sources, hid, _ = make()
    sync.sync("a", hid, dry_run=False)
    g = sync.sync("a", hid, dry_run=False).groups[0]
    assert g.to_create == [] and g.created == [] and len(sources.list()) == 7


def test_a_hospital_without_some_boards_skips_them_and_lists_them():
    sync, sources, hid, _ = make(menu={"NOTI": "공지", "FAQ": "FAQ"})
    g = sync.sync("a", hid, dry_run=False).groups[0]
    assert [s.config["board"] for s in g.created] == ["FAQ"]
    assert g.not_in_menu == ["REGLA", "FORM_BOX", "IRB_LIST", "NEWS", "ETC"]
    assert g.error is None


def test_a_board_that_cannot_be_read_gives_a_reason_and_changes_nothing():
    sync, sources, hid, _ = make(menu=RuntimeError("login rejected"))
    g = sync.sync("a", hid, dry_run=False).groups[0]
    assert g.error and "login rejected" in g.error
    assert g.created == [] and len(sources.list()) == 3


def test_one_failed_creation_does_not_stop_the_others():
    sync, sources, hid, _ = make()
    real = sources.create

    def flaky(actor, name, url, *a, **kw):
        if "board=NEWS" in url:
            raise ValidationError("boom")
        return real(actor, name, url, *a, **kw)

    sources.create = flaky
    g = sync.sync("a", hid, dry_run=False).groups[0]
    assert g.failed == {"NEWS": "boom"}
    assert {s.config["board"] for s in g.created} == {"IRB_LIST", "ETC", "FAQ"}


def test_a_hospital_without_an_r_bay_board_or_unknown_is_refused():
    sync, sources, hid, _ = make()
    with pytest.raises(NotFoundError):
        sync.sync("a", 9999)
    other = sources._hospitals.create("a", "Manual Hospital")
    with pytest.raises(ValidationError):
        sync.sync("a", other.id)
