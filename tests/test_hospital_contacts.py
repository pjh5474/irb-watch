"""Hospital contacts: the adapter read, the reader hook, the service rules, the admin route."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from irb_watch.crawlers.errors import (
    LayoutChangedError,
    LoginExpiredError,
    LoginRejectedError,
    SiteUnavailableError,
)
from irb_watch.crawlers.rbay import RbayAdapter, parse_gate_menus
from irb_watch.crawlers.rbay_reader import RbaySourceReader
from irb_watch.services.contacts import (
    ContactService,
    HospitalContacts,
    StaffRead,
    after_check,
    empty,
    error_kind,
)
from irb_watch.services.errors import NotFoundError, ValidationError
from tests.fakes import FakeAudit
from tests.test_rbay import AGENCY, GATE, SITE, FakePool, Store, fake_site, fetcher

CARDS = """
<html><body><a>로그아웃</a><ul class="ctn_difx_box">
<li class="ct_guide_telbox"><div class="ctn_gdb_text"><p>- 심의: 02-1<br>- 계약: 02-2</p></div></li>
<li class="ct_guide_faxbox"><div class="ctn_gdb_text"></div></li>
<li class="ct_guide_emailbox"><div class="ctn_gdb_text"><p>irb@x.kr</p></div></li></ul></body></html>
"""
NO_CARDS_SIGNED_IN = "<html><body><a>로그아웃</a><p>환영합니다</p></body></html>"
NO_CARDS_SIGNED_OUT = "<html><body><button>입장하기</button></body></html>"
START = f"/agency/main/{AGENCY}"


def site_with_start(start_html, requests=None):
    base = fake_site(requests if requests is not None else [])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == START:
            return httpx.Response(200, text=start_html)
        return base(request)

    return handler


def adapter(start_html):
    return RbayAdapter(fetcher(site_with_start(start_html)), AGENCY, parse_gate_menus(GATE))


# -- the adapter -------------------------------------------------------------------------------


def test_the_adapter_reads_the_three_cards():
    got = adapter(CARDS).contacts()
    assert got.phone == "심의: 02-1\n계약: 02-2" and got.fax == "" and got.email == "irb@x.kr"


def test_a_signed_in_page_with_no_cards_is_a_layout_change_never_no_contact():
    with pytest.raises(LayoutChangedError):
        adapter(NO_CARDS_SIGNED_IN).contacts()


def test_a_signed_out_start_page_is_an_ended_session():
    with pytest.raises(LoginExpiredError):
        adapter(NO_CARDS_SIGNED_OUT).contacts()


# -- the reader hook ---------------------------------------------------------------------------


def reader(start_html, hook):
    return RbaySourceReader(
        FakePool(), Store(), "owner", AGENCY, SITE, "REGLA", 200, 10_000_000,
        lambda s: fetcher(site_with_start(start_html)), hook,
    )


def test_the_hook_runs_after_the_board_read_with_the_same_session():
    seen = []
    result = reader(CARDS, lambda a: seen.append(a.contacts().email)).read(lambda sha, name, c: sha)
    assert seen == ["irb@x.kr"] and len(result.observations) == 2


def test_a_hook_that_raises_never_fails_the_scan_or_ends_the_session():
    def boom(adapter):
        raise LoginExpiredError("contacts said the session ended")

    pool = FakePool()
    r = RbaySourceReader(pool, Store(), "owner", AGENCY, SITE, "REGLA", 200, 10_000_000,
                         lambda s: fetcher(site_with_start(CARDS)), boom)
    result = r.read(lambda sha, name, c: sha)
    assert len(result.observations) == 2
    assert pool.discarded == [] and pool.released == [pool.session]


def test_no_hook_changes_nothing():
    assert len(reader(CARDS, None).read(lambda sha, name, c: sha).observations) == 2


# -- the service -------------------------------------------------------------------------------

NOW = datetime(2026, 11, 17, 4, 0, tzinfo=UTC)


class FakeContactStore:
    def __init__(self, hospitals=(1,)):
        self.rows: dict[int, HospitalContacts] = {}
        self.hospitals = set(hospitals)
        self.fail = False
        self.saved_by = []

    def get(self, hospital_id):
        return self.rows.get(hospital_id)

    def save_scan(self, row):
        # Like the database: only the scan's columns change; the typed ones are kept as stored.
        if self.fail:
            raise RuntimeError("database down")
        kept = self.rows.get(row.hospital_id)
        self.rows[row.hospital_id] = replace(row, manual=kept.manual if kept else {f: None for f in row.manual})
        self.saved_by.append(None)

    def save_manual(self, row, actor_id):
        # Only the typed columns change; the scan's columns are kept as stored.
        if self.fail:
            raise RuntimeError("database down")
        kept = self.rows.get(row.hospital_id)
        self.rows[row.hospital_id] = replace(kept, manual=row.manual) if kept else row
        self.saved_by.append(actor_id)

    def hospital_exists(self, hospital_id):
        return hospital_id in self.hospitals

    def checked_since(self, hospital_id, since):
        row = self.rows.get(hospital_id)
        return bool(row and row.checked_at and row.checked_at >= since)


def make(clock=None):
    store, audit = FakeContactStore(), FakeAudit()
    return ContactService(store, audit, clock=clock or (lambda: NOW)), store, audit


READ = {"phone": "02-1", "fax": "", "email": "a@b.kr"}


def test_a_good_check_saves_the_texts_and_the_dates():
    service, store, _ = make()
    service.record_check(1, READ)
    row = store.rows[1]
    assert row.scanned == READ and row.check_status == "ok" and row.last_ok_at == NOW.isoformat()


def test_a_failed_check_keeps_every_saved_text_and_records_a_short_kind_and_the_detail():
    service, store, _ = make()
    service.record_check(1, READ)
    service.record_check(1, None, SiteUnavailableError("timeout after 20 s"))
    row = store.rows[1]
    assert row.scanned == READ  # not erased
    assert row.check_status == "failed" and row.error_kind == "site_unavailable"
    assert row.error_detail == "timeout after 20 s" and row.last_ok_at == NOW.isoformat()


def test_a_later_good_check_clears_the_failure():
    service, store, _ = make()
    service.record_check(1, None, LayoutChangedError("x"))
    service.record_check(1, READ)
    assert store.rows[1].check_status == "ok" and store.rows[1].error_kind is None
    assert store.rows[1].error_detail is None


def test_a_failure_before_any_good_check_leaves_empty_texts_and_says_failed():
    service, store, _ = make()
    service.record_check(1, None, LayoutChangedError("x"))
    assert store.rows[1].scanned == {"phone": "", "fax": "", "email": ""}
    assert store.rows[1].check_status == "failed" and store.rows[1].last_ok_at is None


@pytest.mark.parametrize(
    "exc, kind",
    [(LoginRejectedError("no"), "login_rejected"), (SiteUnavailableError("x"), "site_unavailable"),
     (LayoutChangedError("x"), "layout_changed"), (RuntimeError("x"), "other"), (LoginExpiredError("x"), "other")],
)
def test_error_kinds(exc, kind):
    assert error_kind(exc) == kind


def test_a_typed_text_is_never_changed_by_a_scan_and_wins_on_screen():
    service, store, _ = make()
    service.set_manual("admin-1", 1, {"phone": "02-999"})
    service.record_check(1, READ)
    row = store.rows[1]
    assert row.manual["phone"] == "02-999" and row.scanned["phone"] == "02-1"
    assert row.shown("phone") == ("02-999", "admin")
    assert row.shown("email") == ("a@b.kr", "r-bay")
    assert row.shown("fax") == ("", "none")


def test_clearing_a_typed_field_shows_the_scanned_text_again():
    service, store, _ = make()
    service.record_check(1, READ)
    service.set_manual("admin-1", 1, {"phone": "02-999"})
    service.set_manual("admin-1", 1, {"phone": "  "})
    assert store.rows[1].manual["phone"] is None and store.rows[1].shown("phone") == ("02-1", "r-bay")


def test_an_edit_that_changes_nothing_writes_and_audits_nothing():
    service, store, audit = make()
    service.set_manual("admin-1", 1, {"phone": "02-9"})
    count = len(audit.entries)
    service.set_manual("admin-1", 1, {"phone": "02-9", "fax": None})
    assert len(audit.entries) == count and len(store.saved_by) == 1


def test_an_edit_is_audited_and_names_the_admin():
    service, store, audit = make()
    service.set_manual("admin-1", 1, {"email": "x@y.kr"})
    assert store.saved_by == ["admin-1"]
    assert audit.entries[-1]["event_type"] == "hospital.contacts_edited"


def test_typed_text_is_tidied_and_limited():
    service, store, _ = make()
    service.set_manual("a", 1, {"phone": "  02-1   (심의) \n\n 02-2  "})
    assert store.rows[1].manual["phone"] == "02-1 (심의)\n02-2"
    with pytest.raises(ValidationError):
        service.set_manual("a", 1, {"phone": "9" * 501})
    with pytest.raises(ValidationError):
        service.set_manual("a", 1, {"address": "x"})


def test_an_unknown_hospital_is_not_found_and_a_known_one_without_a_row_is_empty():
    service, _, _ = make()
    with pytest.raises(NotFoundError):
        service.get(99)
    assert service.get(1) == empty(1)


def test_saving_a_check_never_raises_even_when_the_database_is_down():
    service, store, _ = make()
    store.fail = True
    service.record_check(1, READ)  # no exception: a contact problem never fails a scan


def test_the_cards_are_read_once_per_run_not_once_per_board():
    now = {"t": NOW}
    service, store, _ = make(clock=lambda: now["t"])
    assert service.should_check(1)
    service.record_check(1, READ)
    now["t"] = NOW + timedelta(minutes=5)  # the hospital's next board, minutes later
    assert not service.should_check(1)
    now["t"] = NOW + timedelta(hours=24)  # tomorrow's run
    assert service.should_check(1)


def test_a_failed_check_also_counts_as_this_runs_check():
    service, _, _ = make()
    service.record_check(1, None, LayoutChangedError("x"))
    assert not service.should_check(1)


GROUPS = [{"tab": "IRB", "entries": [{"title": "접수", "name": "간사", "email": "a@b.kr", "tel": "02-1"}]}]


def test_the_manager_page_is_saved_with_the_cards():
    service, store, _ = make()
    service.record_check(1, READ, None, StaffRead(True, GROUPS))
    row = store.rows[1]
    assert row.staff == tuple(GROUPS) and row.staff_status == "ok" and row.check_status == "ok"


def test_a_hospital_that_never_had_a_manager_page_is_none():
    service, store, _ = make()
    service.record_check(1, READ, None, StaffRead(False))
    assert store.rows[1].staff == () and store.rows[1].staff_status == "none"
    assert store.rows[1].staff_error_kind is None


def test_a_failed_manager_read_keeps_the_last_good_staff_and_the_cards_still_save():
    service, store, _ = make()
    service.record_check(1, READ, None, StaffRead(True, GROUPS))
    service.record_check(1, {"phone": "02-new", "fax": "", "email": ""}, None,
                         StaffRead(True, error=LayoutChangedError("cards unreadable")))
    row = store.rows[1]
    assert row.staff == tuple(GROUPS) and row.staff_status == "failed"
    assert row.staff_error_kind == "layout_changed" and row.staff_error_detail == "cards unreadable"
    assert row.scanned["phone"] == "02-new" and row.check_status == "ok"


def test_failed_cards_leave_the_manager_page_as_it_was():
    service, store, _ = make()
    service.record_check(1, READ, None, StaffRead(True, GROUPS))
    service.record_check(1, None, SiteUnavailableError("down"))
    assert store.rows[1].staff == tuple(GROUPS) and store.rows[1].staff_status == "ok"
    assert store.rows[1].check_status == "failed"


def test_a_good_manager_read_clears_its_failure():
    service, store, _ = make()
    service.record_check(1, READ, None, StaffRead(True, error=LayoutChangedError("x")))
    service.record_check(1, READ, None, StaffRead(True, GROUPS))
    row = store.rows[1]
    assert row.staff_status == "ok" and row.staff_error_kind is None and row.staff_error_detail is None


def test_a_start_page_that_no_longer_links_the_manager_page_keeps_the_saved_list_and_says_why():
    service, store, _ = make()
    service.record_check(1, READ, None, StaffRead(True, GROUPS))
    service.record_check(1, READ, None, StaffRead(False))  # the link is missing: changed or partial page
    row = store.rows[1]
    assert row.staff == tuple(GROUPS) and row.staff_status == "failed"
    assert row.staff_error_kind == "layout_changed" and "no longer links" in row.staff_error_detail


def test_an_admin_edit_made_while_a_scan_was_running_is_not_lost():
    service, store, _ = make()
    service.record_check(1, READ)
    # The scan read the row (no typed text yet) and is now busy on the manager page ...
    stale = store.rows[1]
    service.set_manual("admin-1", 1, {"phone": "02-typed"})
    # ... and then saves its result built from the stale copy.
    store.save_scan(after_check(stale, NOW.isoformat(), {"phone": "02-new", "fax": "", "email": ""}))
    assert store.rows[1].manual["phone"] == "02-typed" and store.rows[1].scanned["phone"] == "02-new"


def test_a_scan_that_started_before_an_admin_edit_does_not_hide_the_edit_either_way_round():
    service, store, _ = make()
    service.record_check(1, READ)
    stale_admin = store.rows[1]
    service.record_check(1, {"phone": "02-new", "fax": "", "email": ""})  # a scan finishes first
    store.save_manual(replace(stale_admin, manual={"phone": "02-typed", "fax": None, "email": None}), "admin-1")
    assert store.rows[1].scanned["phone"] == "02-new" and store.rows[1].manual["phone"] == "02-typed"


class RecordingClient:
    """The least of a Supabase client the contact store needs: records each upsert's payload."""

    def __init__(self):
        self.upserts = []

    def table(self, name):
        client = self

        class Table:
            def upsert(self, values, on_conflict=None):
                client.upserts.append((name, values, on_conflict))
                return self

            def execute(self):
                return None

        return Table()


def test_the_database_writes_name_only_their_own_columns():
    from irb_watch.db.contacts import SupabaseContactStore

    client = RecordingClient()
    store = SupabaseContactStore(client)
    row = replace(empty(1), manual={"phone": "02-typed", "fax": None, "email": None})
    store.save_scan(row)
    store.save_manual(row, "admin-1")
    scan_payload, manual_payload = client.upserts[0][1], client.upserts[1][1]
    assert not [k for k in scan_payload if k.startswith("manual_") or k == "updated_by"]
    assert not [k for k in manual_payload if k.startswith(("scanned_", "staff", "check_", "error_"))]
    assert manual_payload["manual_phone"] == "02-typed" and manual_payload["updated_by"] == "admin-1"
    assert client.upserts[0][2] == client.upserts[1][2] == "hospital_id"


def test_after_check_is_pure():
    stored = empty(1)
    out = after_check(stored, "t", READ)
    assert stored == empty(1) and out.scanned == READ
