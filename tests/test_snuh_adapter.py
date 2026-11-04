import httpx
import pytest

from irb_watch.crawlers.errors import (
    FetchError,
    IncompleteScanError,
    LayoutChangedError,
    RobotsDisallowedError,
)
from irb_watch.crawlers.fetcher import SafeFetcher
from irb_watch.crawlers.snuh import SnuhAdapter, parse_detail, parse_list

KEY = "2605277029"


def row(post_id, title, date="2026.09.03", secret="N"):
    return f"""
    <tr data-rgtr-id="u1" data-scrt="{secret}">
      <td class="td-num">1</td>
      <td class="td-title"><a href="javascript:void(0);" onclick="goView('{post_id}', '{secret}', 'u1', 'N');">
        <span class="title-text">{title}</span></a></td>
      <td class="td-date-sm">{date}</td><td class="td-hit">251</td>
    </tr>"""


def list_html(rows, total=None, label="(페이지 1 /2)"):
    total = len(rows) if total is None else total
    return f"""<div><span class="board-list__count">전체 {total} 건 {label}</span>
    <table>{''.join(rows)}</table></div>"""


def detail_html(views=249, prev="이전글 옛날 글 다음글 없음", files=None, body="본문 개정일 2026년 8월"):
    files = files if files is not None else [("a.pdf", "66 KB", "/atch/file/down.do?enc=AAA")]
    links = "".join(
        f'<a class="board-view__file" download href="{h}">{n}'
        f'<span class="board-view__file-size">{s}</span></a>'
        for n, s, h in files
    )
    return f"""<main id="content"><article class="board-view">
      <h2>제목</h2><div>조회 {views} 2026-09-03</div><div>{body}</div>
      <div class="board-view__attachments">첨부파일 {links}</div>
      <nav>{prev}</nav></article></main>"""


# ---- parsing -------------------------------------------------------------


def test_parse_list_reads_ids_titles_dates():
    html = list_html([row("2609030006", "가이드라인 (2026.02.27)"), row("2609030005", "B", "2026.06.03")])
    total, posts = parse_list(html, KEY)
    assert total == 2
    assert [p.post_id for p in posts] == ["2609030006", "2609030005"]
    assert posts[0].title == "가이드라인 (2026.02.27)"
    assert posts[1].posted_on == "2026.06.03"
    assert posts[0].detail_url.endswith(f"/hrpp/bbs/view.do?key={KEY}&pstSn=2609030006")


def test_parse_list_flags_secret_posts():
    _, posts = parse_list(list_html([row("1", "비밀", secret="Y")]), KEY)
    assert posts[0].is_secret is True


def test_parse_list_missing_total_is_layout_change():
    with pytest.raises(LayoutChangedError):
        parse_list("<html><table></table></html>", KEY)


def test_parse_list_declared_posts_but_none_parsed_is_layout_change():
    with pytest.raises(LayoutChangedError):
        parse_list('<span class="board-list__count">전체 7 건</span>', KEY)


def test_parse_list_bad_onclick_is_layout_change():
    bad = row("1", "x").replace("goView('1', 'N', 'u1', 'N')", "goView(1)")
    with pytest.raises(LayoutChangedError):
        parse_list(list_html([bad], total=1), KEY)


def _summary():
    _, posts = parse_list(list_html([row("42", "t")]), KEY)
    return posts[0]


def test_detail_hash_ignores_view_count_and_prev_next():
    a = parse_detail(detail_html(views=10, prev="이전글 A 다음글 B"), _summary())
    b = parse_detail(detail_html(views=9999, prev="이전글 완전히 다른 글 다음글 새 글"), _summary())
    assert a.body_hash == b.body_hash


def test_detail_hash_changes_when_body_changes():
    a = parse_detail(detail_html(body="개정일 2026년 8월"), _summary())
    b = parse_detail(detail_html(body="개정일 2026년 9월"), _summary())
    assert a.body_hash != b.body_hash


def test_detail_attachments_name_size_url():
    d = parse_detail(detail_html(files=[("[공문] x.pdf", "66 KB", "/atch/file/down.do?enc=Z%3D")]), _summary())
    assert d.attachments[0].filename == "[공문] x.pdf"
    assert d.attachments[0].size_label == "66 KB"
    assert d.attachments[0].url == "https://hrpp.snuh.org/atch/file/down.do?enc=Z%3D"
    assert "66 KB" not in d.body_text and "첨부파일" not in d.body_text


def test_detail_without_attachments_is_ok():
    assert parse_detail(detail_html(files=[]), _summary()).attachments == []


def test_detail_missing_article_is_layout_change():
    with pytest.raises(LayoutChangedError):
        parse_detail("<html><body>nope</body></html>", _summary())


def test_detail_unexpected_file_link_is_layout_change():
    with pytest.raises(LayoutChangedError):
        parse_detail(detail_html(files=[("a.pdf", "1 KB", "/other/path")]), _summary())


# ---- adapter end to end (mock transport) ----------------------------------

def RESOLVER(host):
    return ["93.184.216.34"]


def make_adapter(handler):
    fetcher = SafeFetcher(
        transport=httpx.MockTransport(handler),
        resolver=RESOLVER,
        delay=0,
        sleep=lambda s: None,
    )
    return SnuhAdapter(fetcher)


def site(pages, details=None, robots="User-agent: *\nAllow: /", file_bytes=b"%PDF-1.4 x"):
    """pages: {page_number: html}; details: {post_id: html}."""
    details = details or {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if path == "/hrpp/bbs/list.do":
            page = int(request.url.params["pageIndex"])
            return httpx.Response(200, text=pages.get(page, list_html([], total=0)))
        if path == "/hrpp/bbs/view.do":
            return httpx.Response(200, text=details[request.url.params["pstSn"]])
        if path == "/atch/file/down.do":
            return httpx.Response(
                200, content=file_bytes, headers={"content-type": "application-download;charset=utf-8"}
            )
        return httpx.Response(404)

    return handler


def test_scan_board_reads_posts_and_details():
    pages = {1: list_html([row("1", "A"), row("2", "B")], label="(페이지 1 /2)")}
    details = {"1": detail_html(body="aaa"), "2": detail_html(body="bbb")}
    snap = make_adapter(site(pages, details)).scan_board(KEY)
    assert snap.declared_total == 2
    assert [p.summary.post_id for p in snap.posts] == ["1", "2"]
    assert snap.posts[0].body_hash != snap.posts[1].body_hash


def test_scan_board_skips_secret_posts_without_fetching_them():
    pages = {1: list_html([row("1", "A"), row("2", "S", secret="Y")])}
    snap = make_adapter(site(pages, {"1": detail_html()})).scan_board(KEY)
    assert [p.summary.post_id for p in snap.posts] == ["1"]
    assert [s.post_id for s in snap.secret_posts] == ["2"]


def test_follows_pages_until_declared_total_reached():
    pages = {
        1: list_html([row("1", "A")], total=2),
        2: list_html([row("2", "B")], total=2),
    }
    _, posts = make_adapter(site(pages)).list_board(KEY)
    assert [p.post_id for p in posts] == ["1", "2"]


def test_fewer_posts_than_declared_is_incomplete():
    pages = {1: list_html([row("1", "A")], total=7)}
    with pytest.raises(IncompleteScanError):
        make_adapter(site(pages)).list_board(KEY)


def test_empty_board_page_with_declared_posts_is_layout_change():
    pages = {1: '<span class="board-list__count">전체 7 건</span>'}
    with pytest.raises(LayoutChangedError):
        make_adapter(site(pages)).list_board(KEY)


def test_gzip_response_is_decoded_once():
    import gzip

    body = list_html([row("1", "A")]).encode()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(
            200,
            content=gzip.compress(body),
            headers={"content-encoding": "gzip", "content-type": "text/html; charset=utf-8"},
        )

    _, posts = make_adapter(handler).list_board(KEY)
    assert posts[0].post_id == "1"


def test_robots_disallow_blocks_fetch():
    with pytest.raises(RobotsDisallowedError):
        make_adapter(site({}, robots="User-agent: *\nDisallow: /")).list_board(KEY)


def test_http_error_raises_fetch_error():
    adapter = make_adapter(lambda r: httpx.Response(404))
    with pytest.raises(FetchError):
        adapter.list_board(KEY)


def test_redirect_to_internal_host_is_blocked():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(302, headers={"location": "http://internal.example/admin"})

    fetcher = SafeFetcher(
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["10.0.0.5"] if host == "internal.example" else ["93.184.216.34"],
        delay=0,
        sleep=lambda s: None,
    )
    with pytest.raises(FetchError, match="blocked URL"):
        SnuhAdapter(fetcher).list_board(KEY)


def test_download_returns_bytes_and_sha256():
    handler = site({}, file_bytes=b"%PDF-1.4 hello")
    adapter = make_adapter(handler)
    ref = parse_detail(detail_html(), _summary()).attachments[0]
    f = adapter.download(ref)
    assert f.content == b"%PDF-1.4 hello"
    assert len(f.sha256) == 64


def test_download_html_instead_of_file_is_error():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"})

    ref = parse_detail(detail_html(), _summary()).attachments[0]
    with pytest.raises(FetchError):
        make_adapter(handler).download(ref)


def test_download_over_size_cap_is_error():
    ref = parse_detail(detail_html(), _summary()).attachments[0]
    adapter = make_adapter(site({}, file_bytes=b"x" * 100))
    with pytest.raises(FetchError):
        adapter.download(ref, max_bytes=10)


def test_requests_to_same_host_are_spaced():
    sleeps = []
    clock = iter(range(1000))
    fetcher = SafeFetcher(
        transport=httpx.MockTransport(site({1: list_html([])})),
        resolver=RESOLVER,
        delay=2.0,
        sleep=sleeps.append,
        monotonic=lambda: float(next(clock)) * 0.1,
    )
    SnuhAdapter(fetcher).list_board(KEY)
    assert sleeps and all(s > 0 for s in sleeps)
