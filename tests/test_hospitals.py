"""Hospitals group boards (owner, 2026-11-12; docs/decisions.md "Hospitals")."""

import pytest
from fastapi.testclient import TestClient

from irb_watch.api.deps import (
    get_hospital_service,
    get_source_service,
    get_user_store,
    get_verifier,
)
from irb_watch.main import app
from irb_watch.services.docgen import DocChoice, DocgenService
from irb_watch.services.errors import ConflictError, NotFoundError, ValidationError
from irb_watch.services.hospitals import Hospital
from irb_watch.services.sources import SourceService
from tests.fakes import FakeAudit, FakeStore
from tests.fakes_docgen import NOW, FakeDocgenStore, FakeWriter
from tests.fakes_scan import FakeHospitalStore, FakeSourceStore, fake_hospitals
from tests.test_admin_users_api import FakeVerifier

AGENCY = "emNyaVJpcTBZT0RnUmVvL2d6Qlordz09"
OTHER_AGENCY = "b1ZKS1VGblAvTU1hMU5Rb0xWcUp4UT09"


def rbay(agency: str, board: str) -> str:
    return f"https://www.r-bay.co.kr/agency/main/{agency}?board={board}"


def make():
    hospitals = fake_hospitals(FakeHospitalStore())
    audit = FakeAudit()
    sources = SourceService(FakeSourceStore(), audit, lambda url: url, hospitals=hospitals)
    return sources, hospitals, audit


# -- the catalog -------------------------------------------------------------------------------


def test_names_are_unique_ignoring_case_and_spaces():
    _, hospitals, _ = make()
    h = hospitals.create("admin", "  김포우리병원 ")
    assert h.name == "김포우리병원"
    with pytest.raises(ConflictError):
        hospitals.create("admin", "김포우리병원")
    other = hospitals.create("admin", "Ewha  Mokdong")
    assert other.name == "Ewha Mokdong"
    with pytest.raises(ConflictError):
        hospitals.update("admin", h.id, name="EWHA MOKDONG")
    with pytest.raises(ValidationError):
        hospitals.create("admin", "   ")
    with pytest.raises(ValidationError):
        hospitals.create("admin", "x" * 101)
    renamed = hospitals.update("admin", h.id, name="김포우리병원 IRB", note="2층")
    assert (renamed.name, renamed.note) == ("김포우리병원 IRB", "2층")
    assert hospitals.update("admin", h.id, name="김포우리병원 IRB") == renamed  # no change
    with pytest.raises(NotFoundError):
        hospitals.update("admin", 99, name="x")


# -- boards join hospitals ---------------------------------------------------------------------


def test_rbay_boards_of_one_hospital_share_it_and_get_their_kind():
    sources, hospitals, _ = make()
    noti = sources.create("admin", "김포우리병원 : 공지", rbay(AGENCY, "NOTI"), "owner")
    forms = sources.create("admin", "다른 이름 : 양식함", rbay(AGENCY, "FORM_BOX"), "owner")
    rules = sources.create("admin", "규정", rbay(AGENCY, "REGLA"), "owner")
    other = sources.create("admin", "이화목동 : 공지", rbay(OTHER_AGENCY, "NOTI"), "owner")
    assert noti.hospital_id == forms.hospital_id == rules.hospital_id != other.hospital_id
    assert [h.name for h in hospitals.list()] == ["김포우리병원", "이화목동"]
    assert (noti.board_kind, forms.board_kind, rules.board_kind) == ("notice", "forms", "regulations")


def test_snuh_boards_share_one_hospital():
    sources, hospitals, _ = make()
    a = sources.create("admin", "SNUH rules", "https://hrpp.snuh.org/hrpp/bbs/list.do?key=1")
    b = sources.create("admin", "SNUH notices", "https://hrpp.snuh.org/hrpp/bbs/list.do?key=2", board_kind="notice")
    assert a.hospital_id == b.hospital_id and len(hospitals.list()) == 1
    assert (a.board_kind, b.board_kind) == ("other", "notice")


def test_a_chosen_hospital_wins_and_must_exist():
    sources, hospitals, _ = make()
    h = hospitals.create("admin", "서울대학교병원")
    s = sources.create(
        "admin", "Anything : 공지", rbay(AGENCY, "NOTI"), "owner", hospital_id=h.id
    )
    assert s.hospital_id == h.id
    with pytest.raises(NotFoundError, match="hospital"):
        sources.create("admin", "x", rbay(OTHER_AGENCY, "NOTI"), "owner", hospital_id=99)
    with pytest.raises(ValidationError, match="board_kind"):
        sources.create("admin", "x", rbay(OTHER_AGENCY, "NOTI"), "owner", board_kind="memo")


def test_a_manual_source_joins_the_hospital_of_its_name():
    sources, hospitals, _ = make()
    h = hospitals.create("admin", "나은병원")
    s = sources.create_manual("admin", "나은병원", members_only=False)
    assert s.hospital_id == h.id and s.board_kind == "other"
    with pytest.raises(ValidationError, match="choose the hospital"):  # even when the name matches
        sources.create_manual("admin", "나은병원", members_only=True)
    private = sources.create_manual("admin", "Study 7", members_only=True, hospital_id=h.id)
    assert private.hospital_id == h.id
    new = sources.create_manual("admin", "새병원", members_only=False, board_kind="forms")
    assert new.hospital_id != h.id and new.board_kind == "forms"
    assert {x.name for x in hospitals.list()} == {"나은병원", "새병원"}


def test_an_admin_moves_a_board_and_changes_its_kind():
    sources, hospitals, audit = make()
    s = sources.create("admin", "김포우리병원 : 공지", rbay(AGENCY, "NOTI"), "owner")
    target = hospitals.create("admin", "김포우리병원 본원")
    moved = sources.update("admin", s.id, hospital_id=target.id, board_kind="other")
    assert (moved.hospital_id, moved.board_kind) == (target.id, "other")
    assert audit.entries[-1]["payload"] == {"hospital_id": target.id, "board_kind": "other"}
    with pytest.raises(NotFoundError):
        sources.update("admin", s.id, hospital_id=99)
    with pytest.raises(ValidationError):
        sources.update("admin", s.id, board_kind="memo")


def test_a_board_that_is_not_created_leaves_no_hospital_behind():
    sources, hospitals, audit = make()
    url = "https://www.snuh.org/x"  # no adapter: refused before any hospital is made
    with pytest.raises(ValidationError):
        sources.create("admin", "New hospital", url)
    assert hospitals.list() == []
    # A duplicate address is found only when the row is saved: the hospital made for it goes.
    sources._store.create("Other", "rbay", rbay(AGENCY, "NOTI"), {"login_user": "owner"}, None)
    with pytest.raises(ConflictError, match="already registered"):
        sources.create("admin", "김포우리병원 : 공지", rbay(AGENCY, "NOTI"), "owner")
    assert hospitals.list() == []
    assert audit.entries == []  # the hospital entries are in the hospital service's own log
    # A hospital that was there before stays.
    kept = hospitals.create("admin", "이화목동")
    sources._store.create("Dup", "rbay", rbay(OTHER_AGENCY, "NOTI"), {"login_user": "owner"}, None)
    with pytest.raises(ConflictError):
        sources.create("admin", "이화목동 : 공지", rbay(OTHER_AGENCY, "NOTI"), "owner")
    assert hospitals.list() == [kept]


def test_a_long_source_name_still_gets_a_hospital():
    sources, hospitals, _ = make()
    s = sources.create_manual("admin", "가" * 150, members_only=False)
    [h] = hospitals.list()
    assert s.hospital_id == h.id and h.name == "가" * 100
    again = sources.create_manual("admin", "가" * 150, members_only=False)
    assert again.hospital_id == h.id and len(hospitals.list()) == 1


def test_names_match_whatever_the_spaces_and_a_lost_race_uses_the_winner():
    sources, hospitals, _ = make()
    store = hospitals._store
    store.hospitals[1] = Hospital(1, "A  병원")  # as a backfilled row could be: two spaces
    assert hospitals.find_by_name("a 병원").id == 1
    assert sources.create_manual("admin", "A 병원", members_only=False).hospital_id == 1

    # Another request makes the hospital between "not found" and "create".
    real_find, calls = hospitals.find_by_name, []

    def find(name):
        calls.append(name)
        if len(calls) == 1:
            store.create("새병원", "", None)
            return None
        return real_find(name)

    hospitals.find_by_name = find
    made = sources.create_manual("admin", "새병원", members_only=False)
    assert made.hospital_id == real_find("새병원").id and len(calls) == 2


# -- guides follow the hospital ----------------------------------------------------------------


def test_a_hospital_guide_applies_to_documents_from_any_of_its_boards():
    users = FakeStore()
    users.add("user-1", "u@example.com")
    audit = FakeAudit()
    store = FakeDocgenStore(audit)
    store.add_source(1, "김포 : 공지", hospital=10)
    store.add_source(2, "김포 : 양식함", hospital=10)
    store.add_source(3, "이화 : 공지", hospital=20)
    store.add_document(100, 2, "Form")
    store.add_document(200, 3, "Notice")
    store.add_guide(1, "김포 guide", hospitals=[10])
    store.add_guide(2, "이화 guide", hospitals=[20])
    service = DocgenService(users, store, FakeWriter(), audit, daily_limit=3, clock=lambda: NOW)
    assert [g.id for g in service.default_guides("user-1", [2])] == [1]
    assert [g.id for g in service.default_guides("user-1", [1, 3])] == [1, 2]
    draft = service.create_draft("user-1", [DocChoice(100)], None, [], "", [])
    assert draft.draft.guide_ids == (1,)


# -- admin API ---------------------------------------------------------------------------------


ADMIN = {"Authorization": "Bearer tok-admin-1"}
USER = {"Authorization": "Bearer tok-user-1"}


@pytest.fixture
def api():
    users = FakeStore()
    users.add("admin-1", "boss@example.com", role="admin")
    users.add("user-1", "plain@example.com")
    sources, hospitals, _ = make()
    app.dependency_overrides[get_verifier] = lambda: FakeVerifier()
    app.dependency_overrides[get_user_store] = lambda: users
    app.dependency_overrides[get_source_service] = lambda: sources
    app.dependency_overrides[get_hospital_service] = lambda: hospitals
    yield TestClient(app), sources
    app.dependency_overrides.clear()


def test_admin_hospital_api(api):
    client, sources = api
    assert client.get("/admin/hospitals", headers=USER).status_code == 403
    assert client.post("/admin/hospitals", headers=USER, json={"name": "x"}).status_code == 403
    r = client.post("/admin/hospitals", headers=ADMIN, json={"name": "김포우리병원"})
    assert r.status_code == 201
    hid = r.json()["id"]
    assert client.post("/admin/hospitals", headers=ADMIN, json={"name": "김포우리병원"}).status_code == 409
    board = sources.create("admin", "김포우리병원 : 공지", rbay(AGENCY, "NOTI"), "owner")
    assert board.hospital_id == hid  # joins the hospital with the same name
    listed = client.get("/admin/hospitals", headers=ADMIN).json()
    assert listed["board_kinds"] == ["notice", "forms", "regulations", "other"]
    [h] = listed["hospitals"]
    assert h["boards"] == [
        {
            "id": board.id, "name": "김포우리병원 : 공지", "adapter": "rbay", "board_kind": "notice",
            "is_active": True, "requires_login": True,
        }
    ]
    r = client.patch(f"/admin/hospitals/{hid}", headers=ADMIN, json={"name": "김포우리병원 IRB"})
    assert r.status_code == 200 and r.json()["name"] == "김포우리병원 IRB" and len(r.json()["boards"]) == 1
    other = client.post("/admin/hospitals", headers=ADMIN, json={"name": "Other"}).json()["id"]
    r = client.patch(f"/admin/sources/{board.id}", headers=ADMIN, json={"hospital_id": other, "board_kind": "forms"})
    assert r.status_code == 200 and (r.json()["hospital_id"], r.json()["board_kind"]) == (other, "forms")
    assert client.patch(f"/admin/hospitals/{99}", headers=ADMIN, json={"name": "y"}).status_code == 404
