"""One-step registration of an R-BAY hospital's boards (SourceService.create_rbay_hospital)."""

import pytest

from irb_watch.services.errors import ConflictError, ValidationError
from irb_watch.services.sources import SourceService
from tests.fakes import FakeAudit
from tests.fakes_scan import FakeSourceStore, fake_hospitals

CODE = "ZDZxdUxtWlZqRTYwWjRocTUwRWY3UT09"
ADDRESS = f"https://www.r-bay.co.kr/agency/main/{CODE}"


def make():
    hospitals = fake_hospitals()
    store = FakeSourceStore()
    return SourceService(store, FakeAudit(), lambda url: url, hospitals=hospitals), store, hospitals


def register(service, **kw):
    args = {"address": ADDRESS, "hospital_name": "Gachon Hospital", "login_user": "owner",
            "boards": ["NOTI", "FORM_BOX", "REGLA"], "ai_enabled": None}
    return service.create_rbay_hospital("admin-1", **{**args, **kw})


def test_three_boards_become_three_sources_under_one_hospital():
    service, _, hospitals = make()
    sources, hospital_id, made = register(service)
    assert made and len(sources) == 3
    assert {s.hospital_id for s in sources} == {hospital_id}
    assert [s.name for s in sources] == [
        "Gachon Hospital : 공지", "Gachon Hospital : 양식함", "Gachon Hospital : 관련규정"]
    assert [s.board_kind for s in sources] == ["notice", "forms", "regulations"]
    assert all(s.requires_login and s.ai_enabled for s in sources)  # members-only, AI on by default
    assert hospitals.get(hospital_id).name == "Gachon Hospital"


def test_the_urls_are_the_canonical_board_addresses_with_the_login_in_the_config():
    service, _, _ = make()
    sources, _, _ = register(service, boards=["noti"])
    assert sources[0].url == f"{ADDRESS}?board=NOTI"
    assert sources[0].config == {"agency": CODE, "board": "NOTI", "login_user": "owner"}


def test_an_address_with_a_board_or_a_fragment_still_gives_the_hospital():
    service, _, _ = make()
    sources, _, _ = register(service, address=ADDRESS + "#?board=NOTI", boards=["REGLA"])
    assert sources[0].config["board"] == "REGLA"


def test_boards_of_the_same_hospital_join_the_existing_hospital_not_a_new_one():
    service, _, hospitals = make()
    _, first, _ = register(service, boards=["NOTI"])
    sources, second, made = register(service, hospital_name="Another name typed", boards=["REGLA"])
    assert second == first and not made
    assert len(hospitals.list()) == 1
    assert sources[0].name == "Gachon Hospital : 관련규정"  # the hospital's own name wins


def test_a_board_already_registered_for_this_login_refuses_everything():
    service, store, hospitals = make()
    register(service, boards=["NOTI"])
    before = len(store.list())
    with pytest.raises(ConflictError, match=r"NOTI.*nothing was created"):
        register(service, boards=["FORM_BOX", "NOTI"])
    assert len(store.list()) == before  # FORM_BOX was not created either


def test_the_same_boards_for_another_login_are_allowed():
    service, _, _ = make()
    register(service, boards=["NOTI"])
    sources, _, _ = register(service, boards=["NOTI"], login_user="second")
    assert len(sources) == 1


@pytest.mark.parametrize(
    "kw, message",
    [
        ({"address": "https://example.com/agency/main/ZDZxdUxtWlZqRTYwWjRocTUw"}, "R-BAY address"),
        ({"address": "https://www.r-bay.co.kr/main"}, "R-BAY address"),
        ({"hospital_name": "   "}, "hospital name"),
        ({"boards": []}, "at least one board"),
        ({"boards": ["NOTI", "SECRET"]}, "unknown boards: SECRET"),
        ({"login_user": ""}, "login_user"),
        ({"login_user": "../evil"}, "login_user"),
    ],
)
def test_bad_input_creates_nothing_and_says_why(kw, message):
    service, store, hospitals = make()
    with pytest.raises(ValidationError, match=message):
        register(service, **kw)
    assert store.list() == [] and hospitals.list() == []


def test_a_board_listed_twice_is_created_once():
    service, _, _ = make()
    sources, _, _ = register(service, boards=["NOTI", "noti", " NOTI "])
    assert len(sources) == 1


def test_ai_can_be_switched_off_for_all_boards():
    service, _, _ = make()
    sources, _, _ = register(service, ai_enabled=False)
    assert not any(s.ai_enabled for s in sources)


def test_a_failure_while_writing_names_the_boards_that_were_created():
    service, store, _ = make()
    real = store.create
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("database down")
        return real(*a, **kw)

    store.create = flaky
    with pytest.raises(ConflictError, match=r"already created: Gachon Hospital : 공지"):
        register(service)
    assert len(store.list()) == 1


def test_a_failure_on_the_first_board_removes_a_hospital_made_for_it():
    service, store, hospitals = make()

    def broken(*a, **kw):
        raise RuntimeError("database down")

    store.create = broken
    with pytest.raises(RuntimeError):
        register(service)
    assert hospitals.list() == []
