# SNUH HRPP site — scraping reference & agent tool spec

Reference for collecting posts and attachments from the Seoul National University Hospital
임상연구윤리센터 (HRPP) site, `https://hrpp.snuh.org`. Verified 2026-11-04 against board
`key=2605277029` (규정 및 가이드라인).

## TL;DR

- **No JSON API.** Pages are server-rendered HTML (eGovFrame-style `.do` endpoints, jQuery).
  Scrape the HTML with `requests` + `BeautifulSoup`; no headless browser needed.
- **No login / cookies required** for public posts *or* attachment downloads.
- `robots.txt`: `User-agent: * / Allow: /`, sitemap at `/sitemap.xml`.
- Site displays an "이메일주소 무단수집 거부" (no email harvesting) notice — don't extract emails.
- Be polite: ~1 req/sec, identifying User-Agent.

## Endpoints

| Purpose | Request | Notes |
|---|---|---|
| Board list | `GET /hrpp/bbs/list.do?key={boardKey}&pageIndex={n}` | Optional search: `sc` (`pstTtl` title / `pstCn` body / `ttlCn` both), `sw` (term), `sc_bbsCtgrySn` (dept, `0` = all) |
| Post detail | `GET /hrpp/bbs/view.do?key={boardKey}&pstSn={postId}` | Stable, shareable URL |
| Attachment | `GET /atch/file/down.do?enc={token}` | `Content-Type: application-download`. Filename in `Content-Disposition: filename*=UTF-8''<url-encoded>` |
| Secret-post password check | `POST /hrpp/bbs/checkPasswd.do` (`pstSn`, `passwd`) | Out of scope — skip secret posts |

## Known boards (`key`)

All under the same `bbs/list.do` template, so one scraper covers them.

| key | Board |
|---|---|
| 2605277012 | IRB 위원회 명단 |
| 2605277013 | IRB 정규회의 일정 |
| 2605277027 | 공지사항 |
| 2605277029 | 규정 및 가이드라인 ✅ verified |
| 2605277030 | HRPP SOP 등 |
| 2605277031 | IRB (자료실) |
| 2605277032 | QA |
| 2605277033 | 교육 |
| 2605277034 | 연구참여자 |
| 2605277036 | 뉴스레터 |

The template renders columns per-board config (the JSP has many conditional branches), so
other boards may have different columns. **Map cells by `<th>` header text, not position.**
`IRB 위원회 명단` likely contains personal names — decide deliberately whether to collect it.

## HTML selectors

### List page
| Field | Selector / pattern |
|---|---|
| Row | `tbody tr[data-scrt]` (`data-scrt="Y"` = secret post, `data-rgtr-id` = author id) |
| Post id (`pstSn`) | `.td-title a[onclick]` → regex `goView\('(\d+)'` |
| Title | `.title-text` |
| Department | `.td-writer` (header "구분") |
| Date | `.td-date-sm` (`YYYY.MM.DD`) |
| Views | `.td-hit` |
| Has attachment | presence of `svg.icon-attach` in the row |
| Total count | `.board-list__count strong` (first = total, "전체 N건") |

### Detail page
| Field | Selector |
|---|---|
| Title | `.board-view__title` |
| Meta (dept / "조회 N" / `YYYY-MM-DD`) | `.board-view__meta-item` (in that order on verified board) |
| Custom fields | `.board-view__fields` |
| Body (HTML) | `.board-view__content` |
| Attachments | `a.board-view__file` → `href` (relative), text = filename, `.board-view__file-size` = size |

## Gotchas

1. **Page count is wrong.** Board showed "7건 (페이지 1/2)" but page 2 was empty. Paginate
   until a page returns zero `goView` rows instead of trusting the page count.
2. **`enc=` download tokens may not be stable.** Always re-read them from the detail page; store
   `pstSn` + filename + size (or a content hash) as the file's identity, not the URL.
3. Titles embed revision dates, e.g. `…(2024.08.06)(2026.08.13)` — a re-issued guideline may keep
   the same `pstSn` with a new title/date, or get a new post. Detect both (see change detection).
4. Response is UTF-8; filenames are Korean — URL-decode `filename*` and sanitize for Windows
   (`\ / : * ? " < > |`).
5. Skip `data-scrt="Y"` rows; viewing needs a password or the author's login.

## Agent tool spec

Framework-agnostic function tools (fit OpenAI Agents SDK `@function_tool`, or any
JSON-schema tool calling). Keep scraping deterministic inside tools; let the model only decide
*what* to fetch and how to summarize.

### `list_boards() -> Board[]`
Returns the known board table above (`{key, name}`). Static; no network.

### `list_posts(board_key: str, page: int = 1, query: str | None = None, search_in: "title"|"body"|"both" = "title") -> {total: int, posts: PostSummary[]}`
`PostSummary = {pst_sn, title, dept, date, views, has_attachment, is_secret, url}`
Empty `posts` ⇒ no more pages.

### `get_post(board_key: str, pst_sn: str) -> PostDetail`
`PostDetail = {pst_sn, title, dept, date, views, body_text, body_html, url, files: [{name, size_label, download_url}]}`
Truncate `body_text` (e.g. 8k chars) before returning to the model.

### `download_file(download_url: str, dest_dir: str = "downloads") -> {path, filename, bytes, sha256}`
Never pass file bytes to the model — return metadata only. Optionally add a separate
`extract_text(path)` tool (PDF/HWP) if the agent needs to read documents.

### `check_updates(board_keys: str[] | None = None) -> {new: PostSummary[], changed: PostSummary[]}`
The core "irb-watch" tool. Crawls the boards, compares against a local store
(SQLite/JSON keyed by `(board_key, pst_sn)` with `title`, `date`, file `sha256`s), returns
diffs and updates the store. Doing the diff in code (not in the model) keeps it cheap and reliable.

### Sketch (OpenAI Agents SDK, Python)

```python
from agents import Agent, Runner, function_tool

@function_tool
def list_posts(board_key: str, page: int = 1, query: str | None = None) -> dict:
    """List posts on an SNUH HRPP board. Empty 'posts' means no more pages."""
    ...  # requests + BeautifulSoup, selectors above

@function_tool
def get_post(board_key: str, pst_sn: str) -> dict:
    """Get a post's body and attachment list."""
    ...

agent = Agent(
    name="irb-watch",
    model="<your model id>",
    instructions="Monitor SNUH HRPP boards. Use check_updates first; "
                 "summarize new/changed regulations in Korean with links.",
    tools=[list_posts, get_post],  # + list_boards, download_file, check_updates
)
result = Runner.run_sync(agent, "What changed on 규정 및 가이드라인 this week?")
```
Check the current Agents SDK docs for exact signatures — this sketch is unverified.
