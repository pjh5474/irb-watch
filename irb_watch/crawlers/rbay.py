"""Adapter for the boards of a hospital's e-IRB on R-BAY (www.r-bay.co.kr).

Verified live on 2026-11-10 (대구가톨릭대학교의료원, boards REGLA, FORM_BOX, NOTI); every
hospital on R-BAY uses the same board pages (owner, from experience). After the browser login
and 입장하기 (crawlers/rbay_login.py) everything is plain HTTP with the session cookie:

  list   POST /mng/board/<code>/<div>/selectBoardList  selMnuId, currentPage, rowsPerPage
  post   POST /mng/board/<code>/<div>/selectBoard      postsId and the list form's fields
  file   POST /v1/file/fileExistChk (JSON downloadKey), then POST /v1/file/fileDownload

These are the only requests this adapter sends: all of them read. Any page that does not
match the expected markup raises LayoutChangedError, so the source becomes needs_review
instead of looking like "no change".
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from irb_watch.core.hashing import collapse_whitespace, sha256_hex
from irb_watch.core.models import AttachmentRef, DownloadedFile, PostDetail, PostSummary
from irb_watch.crawlers.errors import (
    FetchError,
    IncompleteScanError,
    LayoutChangedError,
    LoginExpiredError,
)
from irb_watch.crawlers.fetcher import FetchedResponse, SafeFetcher

BASE_URL = "https://www.r-bay.co.kr"
HOST = "www.r-bay.co.kr"
HOSTS = (HOST, "r-bay.co.kr")  # hospitals link both; sources are always stored with www
AGENCY_PATH = re.compile(r"^/agency/main/([A-Za-z0-9_\-=]{8,128})$")
# Boards the owner chose (2026-11-10). Others (LAWS, ETC, NEWS, FAQ, ...) can be added later;
# board codes outside this list are refused so the adapter never opens an unknown page.
BOARDS = {"REGLA": "관련규정", "FORM_BOX": "양식함", "NOTI": "공지"}
ROWS_PER_PAGE = 200  # the largest page size the site offers
MAX_PAGES = 20

_BOARD_LINK = re.compile(r"^/mng/board/([A-Za-z_]+)/([A-Za-z]+)/selectBoardList$")
_TOTAL = re.compile(r"총\s*([\d,]+)\s*건")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEY = re.compile(r"^[0-9a-f]{32,128}$")
# A session that is gone answers with the login page or the site's 403 page.
_SESSION_GONE = re.compile(r"/login|/403\.html|/error", re.IGNORECASE)


@dataclass(frozen=True)
class BoardMenu:
    code: str  # e.g. REGLA
    div: str  # e.g. N (part of the path)
    menu_id: str  # selMnuId, differs per hospital

    @property
    def list_path(self) -> str:
        return f"/mng/board/{self.code}/{self.div}/selectBoardList"

    @property
    def post_path(self) -> str:
        return f"/mng/board/{self.code}/{self.div}/selectBoard"


def agency_key(url: str) -> str | None:
    """The hospital part of an agency address, or None if the URL is not one."""
    parts = urlsplit(url.strip())
    if (parts.hostname or "").lower() not in HOSTS:
        return None
    match = AGENCY_PATH.match(parts.path)
    return match.group(1) if match else None


def agency_url(key: str) -> str:
    return f"{BASE_URL}/agency/main/{key}"


def parse_gate_menus(html: str) -> dict[str, BoardMenu]:
    """The board menu of the e-IRB start page (/mng/gate): code -> BoardMenu."""
    soup = BeautifulSoup(html, "html.parser")
    menus: dict[str, BoardMenu] = {}
    for el in soup.select("[data-link]"):
        match = _BOARD_LINK.match(el.get("data-link", ""))
        menu_id = el.get("data-mnu-id", "")
        if match and menu_id.isdigit() and match.group(1) not in menus:
            menus[match.group(1)] = BoardMenu(match.group(1), match.group(2), menu_id)
    if not menus:
        raise LayoutChangedError("e-IRB start page: no board menu found")
    return menus


def _list_table(soup: BeautifulSoup):
    table = soup.select_one("div.list_table table.tc")
    if table is None:
        return None
    heads = [collapse_whitespace(th.get_text()) for th in table.select("thead th")]
    return table if heads[:4] == ["No", "종류", "구분", "제목"] and "등록일" in heads else None


def parse_list(html: str, menu: BoardMenu, agency: str) -> tuple[int, list[PostSummary], dict[str, str]]:
    """Return (declared total, posts on this page, the page's hidden list-form fields)."""
    soup = BeautifulSoup(html, "html.parser")
    table = _list_table(soup)
    total_el = soup.select_one("dl.list_numbox dt")
    match = _TOTAL.search(total_el.get_text(" ")) if total_el else None
    if table is None or match is None:
        raise LayoutChangedError(f"board {menu.code}: list table or total count not found")
    declared = int(match.group(1).replace(",", ""))
    heads = [collapse_whitespace(th.get_text()) for th in table.select("thead th")]
    title_at, date_at = heads.index("제목"), heads.index("등록일")

    posts: dict[str, PostSummary] = {}
    for row in table.select("tbody tr"):
        cells = row.find_all("td", recursive=False)
        ids = {td.get("data-posts-id") for td in cells if td.get("data-posts-id")}
        if not ids:
            continue  # the "no posts" row of an empty board
        if len(ids) != 1 or len(cells) != len(heads):
            raise LayoutChangedError(f"board {menu.code}: a list row does not match the columns")
        post_id = ids.pop()
        if not post_id.isdigit():
            raise LayoutChangedError(f"board {menu.code}: post id {post_id!r} is not a number")
        title = collapse_whitespace(cells[title_at].get_text(" "))
        date = collapse_whitespace(cells[date_at].get_text())
        if not title or not _DATE.match(date):
            raise LayoutChangedError(f"board {menu.code}: post {post_id} has no title or date")
        # Pinned posts can appear twice (top and in place): keep one.
        posts.setdefault(
            post_id,
            PostSummary(
                board_key=menu.code,
                post_id=post_id,
                title=title,
                posted_on=date,
                is_secret=False,
                # Posts open by form POST; this link is the hospital's R-BAY page (log in there).
                detail_url=f"{agency_url(agency)}#{menu.code}-{post_id}",
            ),
        )
    if declared > 0 and not posts:
        raise LayoutChangedError(f"board {menu.code}: declares {declared} posts but none parsed")
    form = soup.select_one("form#dataForm")
    hidden = {
        i.get("name"): i.get("value", "")
        for i in (form.select("input[type=hidden]") if form else [])
        if i.get("name")
    }
    if hidden.get("brdCd") != menu.code or not hidden.get("agencyGrpCd"):
        raise LayoutChangedError(f"board {menu.code}: list form fields not found")
    return declared, list(posts.values()), hidden


def _fields(table) -> dict[str, object]:
    """th label -> the td right after it, for the post's label/value table."""
    out: dict[str, object] = {}
    for th in table.find_all("th"):
        label = collapse_whitespace(th.get_text())
        td = th.find_next_sibling("td")
        if label and td is not None and label not in out:
            out[label] = td
    return out


def parse_detail(html: str, summary: PostSummary) -> PostDetail:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("div.write_table table")
    if table is None:
        raise LayoutChangedError(f"post {summary.post_id}: post table not found")
    fields = _fields(table)
    title_td, body_td = fields.get("제목"), fields.get("내용")
    if title_td is None or body_td is None:
        raise LayoutChangedError(f"post {summary.post_id}: title or body row not found")
    editor = body_td.select_one(".editor_read") or body_td
    # Only the post's own text: author, phone and view counts stay out (personal data, and
    # they would make every view look like a change).
    body = collapse_whitespace(editor.get_text(" "))

    attachments: list[AttachmentRef] = []
    files_td = fields.get("파일")
    if files_td is not None:
        for link in files_td.select("a[data-download-key]"):
            key = link.get("data-download-key", "")
            if not _KEY.match(key):
                raise LayoutChangedError(f"post {summary.post_id}: unexpected download key")
            size_el = link.find_next_sibling("em")
            attachments.append(
                AttachmentRef(
                    filename=collapse_whitespace(link.get_text(" ")),
                    size_label=size_el.get_text(strip=True) if size_el else "",
                    url=key,  # the download key, re-read from the post before every download
                )
            )
    if not body and not attachments:
        raise LayoutChangedError(f"post {summary.post_id}: empty post")
    return PostDetail(
        summary=summary,
        body_text=body,
        body_hash=sha256_hex(body.encode("utf-8")),
        attachments=attachments,
    )


class RbayAdapter:
    """Reads one hospital's boards with a logged-in session (cookies in the fetcher)."""

    def __init__(self, fetcher: SafeFetcher, agency: str, menus: dict[str, BoardMenu]) -> None:
        self._fetcher = fetcher
        self._agency = agency
        self._menus = menus
        self._forms: dict[str, dict[str, str]] = {}

    def close(self) -> None:
        self._fetcher.close()

    def menu(self, code: str) -> BoardMenu:
        if code not in BOARDS:
            raise FetchError(f"board {code} is not on the allowed list")
        menu = self._menus.get(code)
        if menu is None:
            raise LayoutChangedError(f"board {code} is not in this hospital's e-IRB menu")
        return menu

    def _check_session(self, response: FetchedResponse) -> None:
        if _SESSION_GONE.search(urlsplit(response.url).path):
            raise LoginExpiredError(f"R-BAY session ended (got {urlsplit(response.url).path})")

    def _page(self, path: str, data: dict[str, str]) -> FetchedResponse:
        response = self._fetcher.post(f"{BASE_URL}{path}", data=data)
        self._check_session(response)
        return response

    def list_board(self, code: str, max_posts: int = 200) -> tuple[int, list[PostSummary]]:
        menu = self.menu(code)
        seen: dict[str, PostSummary] = {}
        declared = 0
        for page in range(1, MAX_PAGES + 1):
            response = self._page(
                menu.list_path,
                {"selMnuId": menu.menu_id, "currentPage": str(page), "rowsPerPage": str(ROWS_PER_PAGE)},
            )
            declared, posts, hidden = parse_list(response.text, menu, self._agency)
            self._forms[code] = hidden
            new = [p for p in posts if p.post_id not in seen]
            for post in new:
                seen[post.post_id] = post
            if not new or len(seen) >= declared:
                break
        if len(seen) != declared:
            raise IncompleteScanError(f"board {code}: read {len(seen)} posts, site declares {declared}")
        if len(seen) > max_posts:
            raise IncompleteScanError(f"board {code}: {len(seen)} posts exceeds cap {max_posts}")
        return declared, list(seen.values())

    def fetch_post(self, summary: PostSummary) -> PostDetail:
        menu = self.menu(summary.board_key)
        form = self._forms.get(summary.board_key)
        if form is None:
            raise FetchError("read the board list before its posts")
        data = {
            "tranMod": "S",
            "agencyGrpCd": form.get("agencyGrpCd", ""),
            "committeeCd": form.get("committeeCd", ""),
            "brdKind": form.get("brdKind", "board"),
            "brdCd": menu.code,
            "brdDiv": form.get("brdDiv", ""),
            "postsId": summary.post_id,
            "currentPage": "1",
            "ansYn": form.get("ansYn", "N"),
            "boardbrdDiv": form.get("boardbrdDiv", ""),
            "searchCategory": "1",
            "searchTitle": "",
            "rowsPerPage": "20",
        }
        response = self._page(menu.post_path, data)
        return parse_detail(response.text, summary)

    def download(self, attachment: AttachmentRef, max_bytes: int | None = None) -> DownloadedFile:
        key = attachment.url
        if not _KEY.match(key):
            raise FetchError(f"bad download key for {attachment.filename}")
        check = self._fetcher.post(f"{BASE_URL}/v1/file/fileExistChk", json={"downloadKey": key})
        self._check_session(check)
        if '"0000"' not in check.text:
            raise FetchError(f"R-BAY says {attachment.filename} is not available: {check.text[:120]}")
        response = self._fetcher.post(
            f"{BASE_URL}/v1/file/fileDownload",
            data={"downloadKey": key, "downLoadKeys": ""},
            max_bytes=max_bytes,
        )
        self._check_session(response)
        if "text/html" in response.headers.get("content-type", "").lower():
            raise FetchError(f"got an HTML page instead of the file {attachment.filename}")
        if not response.content:
            raise FetchError(f"empty download for {attachment.filename}")
        # The name people see is the post's link text (the served name was the same live).
        return DownloadedFile(attachment.filename, response.content, sha256_hex(response.content))


def logout_form(gate_html: str) -> dict[str, str]:
    """The fields the e-IRB's own logout button sends (form #frmHeader -> /workLogout)."""
    soup = BeautifulSoup(gate_html, "html.parser")
    form = soup.select_one("form#frmHeader")
    if form is None:
        return {}
    return {i.get("name"): i.get("value", "") for i in form.select("input") if i.get("name")}


__all__ = [
    "BASE_URL",
    "BOARDS",
    "BoardMenu",
    "RbayAdapter",
    "agency_key",
    "agency_url",
    "logout_form",
    "parse_detail",
    "parse_gate_menus",
    "parse_list",
]
