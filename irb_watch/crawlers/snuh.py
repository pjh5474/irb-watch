"""Adapter for hrpp.snuh.org bulletin boards (list.do / view.do).

Verified against the live site on 2026-11-04: boards 2605277029 and 2605277034.
Any mismatch with that markup raises LayoutChangedError so the source is marked
`needs_review` instead of looking like "no changes".
"""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from irb_watch.core.hashing import collapse_whitespace, sha256_hex, strip_view_count
from irb_watch.core.models import (
    AttachmentRef,
    BoardSnapshot,
    DownloadedFile,
    PostDetail,
    PostSummary,
)
from irb_watch.crawlers.errors import FetchError, IncompleteScanError, LayoutChangedError
from irb_watch.crawlers.fetcher import SafeFetcher

BASE_URL = "https://hrpp.snuh.org"
MAX_PAGES = 20

_GO_VIEW = re.compile(r"goView\(\s*'([^']+)'\s*,\s*'([YN])'\s*,\s*'([^']*)'\s*,\s*'([YN])'\s*\)")
_TOTAL = re.compile(r"전체\s*([\d,]+)\s*건")
_PREV_NEXT_MARKER = "이전글"


def parse_list(html: str, board_key: str, base_url: str = BASE_URL) -> tuple[int, list[PostSummary]]:
    """Return (declared_total, posts on this page)."""
    soup = BeautifulSoup(html, "html.parser")

    count_el = soup.select_one(".board-list__count")
    match = _TOTAL.search(count_el.get_text(" ", strip=True)) if count_el else None
    if not match:
        raise LayoutChangedError("list page: total count element not found")
    declared_total = int(match.group(1).replace(",", ""))

    posts: list[PostSummary] = []
    for anchor in soup.select("a[onclick*='goView']"):
        parsed = _GO_VIEW.search(anchor.get("onclick", ""))
        title_el = anchor.select_one(".title-text")
        row = anchor.find_parent("tr")
        date_el = row.select_one(".td-date-sm") if row else None
        if not (parsed and title_el and date_el):
            raise LayoutChangedError("list page: row does not match expected markup")
        post_id, secret_flag, _author, _has_password = parsed.groups()
        posts.append(
            PostSummary(
                board_key=board_key,
                post_id=post_id,
                title=collapse_whitespace(title_el.get_text()),
                posted_on=date_el.get_text(strip=True),
                is_secret=secret_flag == "Y",
                detail_url=f"{base_url}/hrpp/bbs/view.do?key={board_key}&pstSn={post_id}",
            )
        )

    if declared_total > 0 and not posts:
        raise LayoutChangedError(f"list page declares {declared_total} posts but none parsed")
    return declared_total, posts


def parse_detail(
    html: str, summary: PostSummary, base_url: str = BASE_URL
) -> PostDetail:
    soup = BeautifulSoup(html, "html.parser")
    article = soup.select_one("article.board-view")
    if article is None:
        raise LayoutChangedError(f"detail page {summary.post_id}: article.board-view not found")

    attachments: list[AttachmentRef] = []
    attachments_box = article.select_one(".board-view__attachments")
    for link in article.select("a.board-view__file"):
        href = link.get("href", "")
        if "/atch/file/down.do" not in href:
            raise LayoutChangedError(f"detail page {summary.post_id}: unexpected file link {href}")
        size_el = link.select_one(".board-view__file-size")
        size_label = size_el.get_text(strip=True) if size_el else ""
        if size_el:
            size_el.extract()
        attachments.append(
            AttachmentRef(
                filename=collapse_whitespace(link.get_text(" ")),
                size_label=size_label,
                url=urljoin(base_url, href),
            )
        )
    if attachments_box is not None:
        attachments_box.extract()

    text = article.get_text(" ")
    cut = text.find(_PREV_NEXT_MARKER)
    if cut != -1:
        text = text[:cut]
    body_text = collapse_whitespace(strip_view_count(text))
    if not body_text:
        raise LayoutChangedError(f"detail page {summary.post_id}: empty body")

    return PostDetail(
        summary=summary,
        body_text=body_text,
        body_hash=sha256_hex(body_text.encode("utf-8")),
        attachments=attachments,
    )


class SnuhAdapter:
    def __init__(self, fetcher: SafeFetcher, base_url: str = BASE_URL) -> None:
        self._fetcher = fetcher
        self._base = base_url

    def close(self) -> None:
        self._fetcher.close()

    def list_board(self, board_key: str, max_posts: int = 200) -> tuple[int, list[PostSummary]]:
        """Read every list page. Raises IncompleteScanError if fewer posts than declared."""
        seen: dict[str, PostSummary] = {}
        declared_total = 0
        for page in range(1, MAX_PAGES + 1):
            response = self._fetcher.get(
                f"{self._base}/hrpp/bbs/list.do",
                params={
                    "key": board_key,
                    "pageIndex": str(page),
                    "sc_bbsCtgrySn": "0",
                    "sc": "pstTtl",
                    "sw": "",
                },
            )
            declared_total, posts = parse_list(response.text, board_key, self._base)
            new = [p for p in posts if p.post_id not in seen]
            for post in new:
                seen[post.post_id] = post
            # The site's "page x/y" label is unreliable; stop on a page with nothing new.
            if not new or len(seen) >= declared_total:
                break

        if len(seen) != declared_total:
            raise IncompleteScanError(
                f"board {board_key}: read {len(seen)} posts, site declares {declared_total}"
            )
        if len(seen) > max_posts:
            raise IncompleteScanError(f"board {board_key}: {len(seen)} posts exceeds cap {max_posts}")
        return declared_total, list(seen.values())

    def fetch_post(self, summary: PostSummary) -> PostDetail:
        response = self._fetcher.get(summary.detail_url)
        return parse_detail(response.text, summary, self._base)

    def scan_board(self, board_key: str, max_posts: int = 200) -> BoardSnapshot:
        declared_total, summaries = self.list_board(board_key, max_posts)
        details = [self.fetch_post(s) for s in summaries if not s.is_secret]
        secrets = [s for s in summaries if s.is_secret]
        return BoardSnapshot(board_key, declared_total, details, secrets)

    def download(self, attachment: AttachmentRef, max_bytes: int | None = None) -> DownloadedFile:
        """Download one attachment. Use a URL freshly read from the detail page."""
        response = self._fetcher.get(attachment.url, max_bytes=max_bytes)
        if "text/html" in response.headers.get("content-type", "").lower():
            raise FetchError(f"got an HTML page instead of a file for {attachment.filename}")
        if not response.content:
            raise FetchError(f"empty download for {attachment.filename}")
        return DownloadedFile(
            filename=attachment.filename,
            content=response.content,
            sha256=sha256_hex(response.content),
        )
