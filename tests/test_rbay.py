"""R-BAY boards: page parsing (trimmed copies of the live pages), the adapter's requests, the
shared login pool, the reader, and source registration. No network, no browser."""

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from irb_watch.core.models import AttachmentRef, PostSummary
from irb_watch.crawlers.credentials import Credentials
from irb_watch.crawlers.errors import (
    FetchError,
    IncompleteScanError,
    LayoutChangedError,
    LoginExpiredError,
    LoginRejectedError,
    SiteUnavailableError,
)
from irb_watch.crawlers.fetcher import SafeFetcher
from irb_watch.crawlers.rbay import (
    BASE_URL,
    BoardMenu,
    RbayAdapter,
    agency_key,
    logout_form,
    parse_detail,
    parse_gate_menus,
    parse_list,
)
from irb_watch.crawlers.rbay_login import (
    IDLE_LOGOUT,
    SESSION_REUSE,
    EntryRefusedError,
    Lease,
    RbaySession,
    RbaySessionPool,
    logout,
)
from irb_watch.crawlers.rbay_reader import RbaySourceReader
from irb_watch.services.errors import ValidationError
from irb_watch.services.sources import RBAY_ADAPTER, SourceService, detect_adapter
from tests.fakes import FakeAudit
from tests.fakes_scan import FakeSourceStore

FIX = Path(__file__).parent / "fixtures" / "rbay"
GATE = (FIX / "gate.html").read_text(encoding="utf-8")
LIST = (FIX / "list_REGLA.html").read_text(encoding="utf-8")
DETAIL = (FIX / "detail_1842962.html").read_text(encoding="utf-8")
AGENCY = "emNyaVJpcTBZT0RnUmVvL2d6Qlordz09"
REGLA = BoardMenu("REGLA", "N", "2604000000")
PDF_KEY = "aeef8f7775d19902fcff2da70cf41f58294474d8083663cbd1e07efb2f5039e8"
ZIP_KEY = "bbbb8f7775d19902fcff2da70cf41f58294474d8083663cbd1e07efb2f5039e8"


def summary(post_id="1842962"):
    return PostSummary("REGLA", post_id, "제목", "2026-06-11", False, f"{BASE_URL}/x#{post_id}")


# -- parsing ---------------------------------------------------------------------------


def test_gate_menu_gives_each_board_its_path_and_menu_id():
    menus = parse_gate_menus(GATE)
    assert menus["REGLA"] == REGLA
    assert menus["NOTI"].list_path == "/mng/board/NOTI/NT/selectBoardList"
    assert menus["FORM_BOX"].menu_id == "2605000000"
    assert "selectPjtList" not in str(menus)  # study pages are not boards


def test_gate_without_menu_is_a_layout_change():
    with pytest.raises(LayoutChangedError):
        parse_gate_menus("<html><body>점검 중</body></html>")


def test_list_reads_posts_titles_dates_and_the_form():
    declared, posts, form = parse_list(LIST, REGLA, AGENCY)
    assert declared == 2
    assert [p.post_id for p in posts] == ["1842962", "1842945"]
    assert posts[0].title == "임상시험대상자 병력 및 병용약물 확인 관련 안내"
    assert posts[1].title.startswith("임상시험 대상자 동의 설명서")  # the pin icon is ignored
    assert posts[0].posted_on == "2026-06-11"
    assert posts[0].detail_url == f"{BASE_URL}/agency/main/{AGENCY}#REGLA-1842962"
    assert form["agencyGrpCd"] == "DCUMC" and form["brdCd"] == "REGLA"


def test_list_counts_a_pinned_post_once():
    row_start = LIST.index("<tr class=")
    row_end = LIST.index("</tr>", row_start) + len("</tr>")
    doubled = LIST[:row_end] + LIST[row_start:row_end] + LIST[row_end:]
    _, posts, _ = parse_list(doubled, REGLA, AGENCY)
    assert [p.post_id for p in posts] == ["1842962", "1842945"]


def test_list_changes_are_layout_changes():
    with pytest.raises(LayoutChangedError):
        parse_list(LIST.replace("목록 총", "합계"), REGLA, AGENCY)
    with pytest.raises(LayoutChangedError):
        parse_list(LIST.replace("<th>등록일</th>", "<th>날짜</th>"), REGLA, AGENCY)
    with pytest.raises(LayoutChangedError, match="none parsed"):
        parse_list(LIST.replace("data-posts-id", "data-x"), REGLA, AGENCY)
    with pytest.raises(LayoutChangedError, match="form"):
        parse_list(LIST.replace('value="REGLA"', 'value="NOTI"'), REGLA, AGENCY)


def test_an_empty_board_is_fine():
    empty = LIST.replace("목록 총 2건", "목록 총 0건").replace("data-posts-id", "data-x")
    declared, posts, _ = parse_list(empty, REGLA, AGENCY)
    assert (declared, posts) == (0, [])


def test_detail_keeps_only_the_post_text_and_its_files():
    detail = parse_detail(DETAIL, summary())
    assert detail.body_text.startswith("1. 임상시험을 하려는 자는")
    assert detail.body_text.endswith("끝.")
    for left_out in ("유*옥", "053-000-0000", "조회 24", "공개"):  # author, phone, views, flags
        assert left_out not in detail.body_text
    assert [(a.filename, a.size_label, a.url) for a in detail.attachments] == [
        ("(공문)임상시험대상자 병력 및 병용약물 확인 관련 안내.pdf", "0.06MB", PDF_KEY),
        ("IRB Submission.zip", "7.38MB", ZIP_KEY),
    ]


def test_detail_changes_are_layout_changes():
    with pytest.raises(LayoutChangedError):
        parse_detail(DETAIL.replace("write_table", "other"), summary())
    with pytest.raises(LayoutChangedError):
        parse_detail(DETAIL.replace("<th>내용</th>", "<th>본문</th>"), summary())
    with pytest.raises(LayoutChangedError, match="download key"):
        parse_detail(DETAIL.replace(PDF_KEY, "../../etc"), summary())


def test_logout_form_comes_from_the_gate_page():
    form = logout_form(GATE)
    assert form["agencyGrpCd"] == "DCUMC" and form["committeeCd"] == "IRB"


# -- the adapter's requests --------------------------------------------------------------


def fake_site(requests, *, list_html=LIST, detail_html=DETAIL, file_bytes=b"%PDF-1.4 x", session_gone=False):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = request.content.decode()
        requests.append((request.method, path, body))
        if path == "/robots.txt":
            return httpx.Response(404)
        if path == "/login":
            return httpx.Response(200, text="<input type=password>")
        if session_gone:
            return httpx.Response(302, headers={"location": "/login"})
        if path.endswith("/selectBoardList"):
            return httpx.Response(200, text=list_html)
        if path.endswith("/selectBoard"):
            return httpx.Response(200, text=detail_html)
        if path == "/v1/file/fileExistChk":
            return httpx.Response(200, json={"resultCode": "0000", "resultMsg": "ok"})
        if path == "/v1/file/fileDownload":
            return httpx.Response(200, content=file_bytes, headers={"content-type": "application/octet-stream"})
        return httpx.Response(404)

    return handler


def fetcher(handler):
    return SafeFetcher(
        transport=httpx.MockTransport(handler), resolver=lambda h: ["93.184.216.34"], delay=0, sleep=lambda s: None
    )


def adapter(requests, **kw):
    return RbayAdapter(fetcher(fake_site(requests, **kw)), AGENCY, parse_gate_menus(GATE))


def test_list_and_post_are_form_posts_with_the_menu_id_and_page_size():
    requests = []
    a = adapter(requests)
    declared, posts = a.list_board("REGLA")
    detail = a.fetch_post(posts[0])
    assert declared == 2 and detail.attachments
    method, path, body = requests[1]
    assert (method, path) == ("POST", "/mng/board/REGLA/N/selectBoardList")
    assert parse_qs(body) == {"selMnuId": ["2604000000"], "currentPage": ["1"], "rowsPerPage": ["200"]}
    method, path, body = requests[2]
    sent = parse_qs(body, keep_blank_values=True)
    assert (method, path) == ("POST", "/mng/board/REGLA/N/selectBoard")
    assert sent["postsId"] == ["1842962"] and sent["brdCd"] == ["REGLA"] and sent["agencyGrpCd"] == ["DCUMC"]


def test_download_checks_then_downloads_by_key():
    requests = []
    a = adapter(requests)
    got = a.download(AttachmentRef("a.pdf", "1KB", PDF_KEY))
    assert got.content == b"%PDF-1.4 x" and len(got.sha256) == 64
    assert [r[1] for r in requests[-2:]] == ["/v1/file/fileExistChk", "/v1/file/fileDownload"]
    assert json.loads(requests[-2][2]) == {"downloadKey": PDF_KEY}
    assert parse_qs(requests[-1][2], keep_blank_values=True)["downloadKey"] == [PDF_KEY]


def test_an_html_page_instead_of_a_file_is_an_error():
    def handler(request):
        if request.url.path == "/v1/file/fileDownload":
            return httpx.Response(200, text="<html>no</html>", headers={"content-type": "text/html"})
        return fake_site([])(request)

    a = RbayAdapter(fetcher(handler), AGENCY, parse_gate_menus(GATE))
    with pytest.raises(FetchError, match="HTML page"):
        a.download(AttachmentRef("a.pdf", "1KB", PDF_KEY))


def test_a_file_the_site_says_is_missing_is_not_downloaded():
    requests = []

    def handler(request):
        if request.url.path == "/v1/file/fileExistChk":
            requests.append("check")
            return httpx.Response(200, json={"resultCode": "9999", "resultMsg": "파일 없음"})
        if request.url.path == "/v1/file/fileDownload":
            requests.append("download")
        return fake_site([])(request)

    a = RbayAdapter(fetcher(handler), AGENCY, parse_gate_menus(GATE))
    with pytest.raises(FetchError, match="not available"):
        a.download(AttachmentRef("a.pdf", "1KB", PDF_KEY))
    assert requests == ["check"]


def test_a_bad_download_key_is_never_sent():
    requests = []
    with pytest.raises(FetchError):
        adapter(requests).download(AttachmentRef("a.pdf", "1KB", "../x"))
    assert not any(r[1].startswith("/v1/") for r in requests)


def test_an_ended_session_is_a_login_problem_not_a_layout_change():
    with pytest.raises(LoginExpiredError):
        adapter([], session_gone=True).list_board("REGLA")


def test_only_allowed_boards_are_opened():
    requests = []
    with pytest.raises(FetchError, match="allowed"):
        adapter(requests).list_board("IRB_LIST")
    assert requests == []


def test_fewer_posts_than_declared_fails_the_scan():
    short = LIST.replace("목록 총 2건", "목록 총 3건")
    with pytest.raises(IncompleteScanError):
        adapter([], list_html=short).list_board("REGLA")


def test_post_before_list_is_refused():
    with pytest.raises(FetchError):
        adapter([]).fetch_post(summary())


# -- the login pool ----------------------------------------------------------------------


DEFAULT_CREDS = Credentials("id", "pw")


class Store:
    def __init__(self, creds=DEFAULT_CREDS):
        self.creds = creds

    def get(self, user, site):
        assert site == "rbay"
        return self.creds

    def set(self, user, site, credentials):
        self.creds = credentials


class Timers:
    def __init__(self):
        self.started = []

    def __call__(self, seconds, action):
        timer = FakeTimer(seconds, action)
        self.started.append(timer)
        return timer


class FakeTimer:
    def __init__(self, seconds, action):
        self.seconds, self.action, self.cancelled = seconds, action, False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.action()


def new_session(agency=AGENCY):
    return RbaySession("", agency, [{"name": "SESSION", "value": "s"}], "UA", GATE, parse_gate_menus(GATE))


def make_pool(outcome=None):
    """outcome(agency) -> None for a login that works, or an exception to raise."""
    now = [0.0]
    logins, logouts, timers = [], [], Timers()

    def login(agency, creds):
        logins.append((agency, creds.password))
        problem = outcome(agency) if outcome else None
        if problem:
            raise problem
        return new_session(agency)

    pool = RbaySessionPool(login, lambda s: logouts.append(s) or True, lambda: now[0], timers)
    return pool, now, logins, logouts, timers


def test_the_boards_of_one_hospital_share_one_login():
    pool, now, logins, logouts, timers = make_pool()
    store = Store()
    first = pool.acquire("owner", AGENCY, store)
    assert first.fresh
    pool.release(first.session)
    now[0] += 30
    second = pool.acquire("owner", AGENCY, store)
    assert second.session is first.session and not second.fresh and len(logins) == 1
    assert timers.started[0].cancelled  # the pending logout was called off
    pool.release(second.session)
    timers.started[-1].fire()
    assert timers.started[-1].seconds == IDLE_LOGOUT
    assert logouts == [first.session] and first.session.closed


def test_an_old_idle_session_is_logged_out_and_replaced():
    pool, now, logins, logouts, _ = make_pool()
    first = pool.acquire("owner", AGENCY, Store())
    pool.release(first.session)
    now[0] += SESSION_REUSE + 1
    second = pool.acquire("owner", AGENCY, Store())
    assert second.session is not first.session and len(logins) == 2 and logouts == [first.session]


def test_a_session_in_use_is_never_logged_out_by_a_late_timer():
    pool, now, logins, logouts, timers = make_pool()
    lease = pool.acquire("owner", AGENCY, Store())
    pool.release(lease.session)
    late = timers.started[-1]
    # The next board takes the session just as the timer fires: the cancel comes too late
    # for a callback already running, so the callback itself must see the session is taken.
    again = pool.acquire("owner", AGENCY, Store())
    late.action()
    assert logouts == [] and not again.session.closed


def test_a_rejected_password_is_not_sent_again_but_a_new_one_is():
    pool, now, logins, _, _ = make_pool(lambda agency: LoginRejectedError("R-BAY login rejected: 비밀번호 오류"))
    store = Store()
    with pytest.raises(LoginRejectedError):
        pool.acquire("owner", AGENCY, store)
    with pytest.raises(LoginRejectedError, match="not tried again"):
        pool.acquire("owner", "OTHERHOSPITAL", store)
    assert len(logins) == 1  # the second board did not try the same password
    store.creds = Credentials("id", "new-password")
    with pytest.raises(LoginRejectedError):
        pool.acquire("owner", AGENCY, store)
    assert len(logins) == 2  # a changed password is tried at once


def test_one_hospital_refusing_entry_does_not_block_another():
    refused = EntryRefusedError("this R-BAY account has no 입장하기 for this hospital")
    pool, now, logins, _, _ = make_pool(lambda agency: refused if agency == "B" else None)
    with pytest.raises(EntryRefusedError):
        pool.acquire("owner", "B", Store())
    assert pool.acquire("owner", AGENCY, Store()).fresh  # hospital A still logs in
    with pytest.raises(LoginRejectedError, match="not tried again"):
        pool.acquire("owner", "B", Store())  # B itself is remembered
    assert [a for a, _ in logins] == ["B", AGENCY]


def test_a_scan_a_person_started_tries_a_remembered_login_again():
    answers = [LoginRejectedError("R-BAY login rejected: 잠시 후 다시"), None]
    pool, now, logins, _, _ = make_pool(lambda agency: answers.pop(0))
    with pytest.raises(LoginRejectedError):
        pool.acquire("owner", AGENCY, Store())
    with pytest.raises(LoginRejectedError, match="start a scan by hand"):
        pool.acquire("owner", AGENCY, Store())
    assert pool.acquire("owner", AGENCY, Store(), person_started=True).fresh
    assert len(logins) == 2


def test_site_trouble_is_not_remembered_as_a_rejection():
    answers = [SiteUnavailableError("R-BAY could not be reached"), None]
    pool, *_ = make_pool(lambda agency: answers.pop(0))
    with pytest.raises(SiteUnavailableError):
        pool.acquire("owner", AGENCY, Store())
    assert pool.acquire("owner", AGENCY, Store()).fresh  # tried again at once


def test_missing_credentials_are_a_rejection_with_a_reason():
    pool, *_ = make_pool()
    with pytest.raises(LoginRejectedError, match="no stored R-BAY login"):
        pool.acquire("owner", AGENCY, Store(None))


def test_a_discarded_session_is_never_handed_out_again():
    pool, now, logins, _, _ = make_pool()
    first = pool.acquire("owner", AGENCY, Store())
    pool.discard(first.session)
    pool.release(first.session)
    assert pool.acquire("owner", AGENCY, Store()).session is not first.session and len(logins) == 2


def test_logout_posts_the_eirb_header_form_and_reports_a_refusal():
    sent = []

    def site(status, location):
        def handler(request):
            sent.append((request.url.path, request.content.decode()))
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(status, headers={"location": location})

        return handler

    session = RbaySession("owner", AGENCY, [], "UA", GATE, {})
    assert logout(session, lambda s: fetcher(site(302, "/main"))) is True
    assert sent[-1][0] == "/workLogout" and "agencyGrpCd=DCUMC" in sent[-1][1]
    assert logout(session, lambda s: fetcher(site(302, "/403.html"))) is False


# -- the reader --------------------------------------------------------------------------


class FakePool:
    def __init__(self, fresh=(True,)):
        self.session = RbaySession("owner", AGENCY, [], "UA", GATE, parse_gate_menus(GATE))
        self.released, self.discarded, self.person = [], [], []
        self._fresh = list(fresh)

    def acquire(self, user, agency, credentials, person_started=False):
        self.person.append(person_started)
        return Lease(self.session, self._fresh.pop(0) if self._fresh else True)

    def release(self, session):
        self.released.append(session)

    def discard(self, session):
        self.discarded.append(session)


def zip_bytes():
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("protocol.txt", "hello")
        z.writestr("__MACOSX/._protocol.txt", "junk")
    return buffer.getvalue()


def test_reader_turns_a_board_into_observations_and_unpacks_zips():
    def handler(request):
        if request.url.path == "/v1/file/fileDownload":
            key = parse_qs(request.content.decode())["downloadKey"][0]
            content = zip_bytes() if key == ZIP_KEY else b"%PDF-1.4 x"
            return httpx.Response(200, content=content, headers={"content-type": "application/octet-stream"})
        return fake_site([])(request)

    pool = FakePool()
    stored = []
    reader = RbaySourceReader(pool, Store(), "owner", AGENCY, "REGLA", 200, 10_000_000, lambda s: fetcher(handler))
    reading = reader.read(lambda sha, name, content: stored.append(name) or sha)
    assert reading.declared_total == 2 and len(reading.observations) == 2
    obs = reading.observations[0]
    assert obs.external_id == "1842962" and obs.posted_on == "2026-06-11"
    names = [f.filename for f in obs.content.files]
    assert "IRB Submission.zip" in names and "IRB Submission.zip/protocol.txt" in names
    assert not any("__MACOSX" in n for n in names)
    assert "IRB Submission.zip/protocol.txt" in stored  # each file inside is stored on its own
    assert pool.released == [pool.session] and pool.discarded == []


def test_reader_drops_a_session_that_ended():
    pool = FakePool()
    reader = RbaySourceReader(
        pool, Store(), "owner", AGENCY, "REGLA", 200, 10_000_000, lambda s: fetcher(fake_site([], session_gone=True))
    )
    with pytest.raises(LoginExpiredError):
        reader.read(lambda sha, name, content: sha)
    assert pool.discarded == [pool.session] and pool.released == [pool.session]
    assert pool.person == [False]  # a fresh login that fails is not retried


def test_reader_logs_in_again_when_a_handed_over_session_had_expired():
    calls = []

    def make(session):
        calls.append(session)
        # The first (reused) session gets the login page; the second (fresh) one works.
        return fetcher(fake_site([], session_gone=len(calls) == 1))

    pool = FakePool(fresh=(False, True))
    reader = RbaySourceReader(pool, Store(), "owner", AGENCY, "REGLA", 200, 10_000_000, make)
    reader.person_started()
    reading = reader.read(lambda sha, name, content: sha)
    assert len(reading.observations) == 2
    assert len(pool.person) == 2 and all(pool.person)  # "a person started it" is passed on
    assert pool.discarded == [pool.session] and len(pool.released) == 2


def test_tls_context_never_trusts_the_shipped_intermediate_alone():
    import ssl

    from irb_watch.crawlers.tls import rbay_ssl_context

    context = rbay_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert not context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN


# -- registering a source ----------------------------------------------------------------


def test_an_rbay_board_address_becomes_a_source():
    adapter_name, url, config = detect_adapter(f"https://www.r-bay.co.kr/agency/main/{AGENCY}?board=regla")
    assert adapter_name == RBAY_ADAPTER
    assert url == f"https://www.r-bay.co.kr/agency/main/{AGENCY}?board=REGLA"
    assert config == {"agency": AGENCY, "board": "REGLA"}
    assert agency_key("https://www.r-bay.co.kr/main") is None
    # Hospitals also link the address without www; it is stored with www.
    _, url, _ = detect_adapter("https://r-bay.co.kr/agency/main/b1ZKS1VGblAvTU1hMU5Rb0xWcUp4UT09?board=NOTI")
    assert url == "https://www.r-bay.co.kr/agency/main/b1ZKS1VGblAvTU1hMU5Rb0xWcUp4UT09?board=NOTI"


def test_an_rbay_source_always_has_a_login():
    service = SourceService(FakeSourceStore(), FakeAudit(), lambda url: url)
    url = f"https://www.r-bay.co.kr/agency/main/{AGENCY}?board=NOTI"
    with pytest.raises(ValidationError, match="login_user"):
        service.create("admin", "공지", url)
    source = service.create("admin", "공지", url, login_user="owner", ai_enabled=True)
    assert source.requires_login and source.ai_enabled and source.adapter == RBAY_ADAPTER
    with pytest.raises(ValidationError, match="without a login"):
        service.update("admin", source.id, login_user="")


def test_post_follows_a_redirect_as_a_get_and_tls_checks_stay_on():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/form":
            return httpx.Response(302, headers={"location": "/done"})
        return httpx.Response(200, text="ok")

    assert fetcher(handler).post(f"{BASE_URL}/form", data={"a": "1"}).text == "ok"
    assert seen[-2:] == [("POST", "/form"), ("GET", "/done")]
    with pytest.raises(ValueError):
        SafeFetcher(verify=False)


@pytest.mark.parametrize(
    "url, message",
    [
        (f"https://www.r-bay.co.kr/agency/main/{AGENCY}", "board"),
        (f"https://www.r-bay.co.kr/agency/main/{AGENCY}?board=IRB_LIST", "board"),
        ("https://www.r-bay.co.kr/mng/gate?board=REGLA", "hospital's address"),
    ],
)
def test_bad_rbay_addresses_say_what_is_missing(url, message):
    with pytest.raises(ValidationError, match=message):
        detect_adapter(url)
