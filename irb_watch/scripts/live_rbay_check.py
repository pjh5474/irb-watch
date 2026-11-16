"""Read-only live check of the R-BAY adapter. Writes nothing to the database.

    uv run python -m irb_watch.scripts.live_rbay_check --user owner \
        --url "https://www.r-bay.co.kr/agency/main/<code>?board=REGLA"

Uses the stored login (Credential Manager with --keyring, else the server's store) for the
hospital, found the same way scans find it (login_site, then the old shared 'rbay' name).
One login: lists the board, opens its first post, downloads that post's first file, reads its
text, then logs out with the e-IRB's own logout and checks that it worked. Prints counts and
names only, never the password.
"""

import argparse
import hashlib
import sys
from contextlib import closing
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from irb_watch.api.deps import get_credential_store
from irb_watch.crawlers.attachments import read_attachment
from irb_watch.crawlers.credentials import KeyringCredentialStore
from irb_watch.crawlers.fetcher import SafeFetcher
from irb_watch.crawlers.rbay import BASE_URL, HOST, RbayAdapter, _fields
from irb_watch.crawlers.rbay_login import browser_enter, logout, rbay_site_name, stored_login
from irb_watch.crawlers.tls import rbay_ssl_context
from irb_watch.services.sources import detect_adapter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only live check of one R-BAY board.")
    parser.add_argument("--user", required=True, help="login label, e.g. owner")
    parser.add_argument("--url", required=True, help="hospital R-BAY address with ?board=<code>")
    parser.add_argument(
        "--keyring", action="store_true", help="read the login from Credential Manager"
    )
    parser.add_argument("--post", help="check this post id instead of the first with files")
    parser.add_argument(
        "--images", action="store_true",
        help="with --post: list the images in the post's text and fetch the first one three "
             "times (twice with the session, once without)",
    )
    args = parser.parse_args(argv)
    if args.images and not args.post:
        parser.error("--images needs --post")

    _, _, config = detect_adapter(args.url)
    store = KeyringCredentialStore() if args.keyring else get_credential_store()
    credentials, site = stored_login(store, args.user, rbay_site_name(config["agency"]))
    if credentials is None:
        print(f"No stored login for '{args.user}' under site '{site}'.")
        return 1
    print(f"0. using the login stored under site '{site}'")

    session = browser_enter(config["agency"], credentials)
    print(f"1. logged in and entered the e-IRB; boards in its menu: {sorted(session.menus)}")

    def make() -> SafeFetcher:
        return SafeFetcher(cookies=session.jar(), headers=session.headers(), verify=rbay_ssl_context())

    ok = True
    adapter = RbayAdapter(make(), config["agency"], session.menus)
    try:
        declared, posts = adapter.list_board(config["board"])
        print(f"2. board {config['board']}: {declared} posts declared, {len(posts)} read")
        chosen = [p for p in posts if p.post_id == args.post] if args.post else posts[:10]
        if args.post and not chosen:
            ok = False
            print(f"3. post {args.post} is not on this board")
        elif args.images:
            ok = check_images(adapter, chosen[0], session)
        else:
            check_download(adapter, chosen)
    except Exception as exc:  # noqa: BLE001 - a check script reports any failure, then logs out
        ok = False
        print(f"FAILED: {type(exc).__name__}: {exc}")
    finally:
        adapter.close()
        accepted = logout(session)
        check = make()
        try:
            page = check.get(f"{BASE_URL}/main")
            still_in = "로그아웃" in page.text
        finally:
            check.close()
        print(f"5. logout accepted: {accepted}; logged out (no 로그아웃 on /main): {not still_in}")
        ok = ok and accepted and not still_in
    return 0 if ok else 1


def check_download(adapter: RbayAdapter, chosen) -> None:
    """The first post with files among `chosen`: download its first file and read it."""
    with_files = next((d for d in (adapter.fetch_post(p) for p in chosen) if d.attachments), None)
    if with_files is None:
        print("3. no file in the first 10 posts; download not checked")
        return
    ref = with_files.attachments[0]
    print(f"3. post {with_files.summary.post_id}: {len(with_files.body_text)} chars, "
          f"{len(with_files.attachments)} files")
    got = adapter.download(ref)
    records, warnings = read_attachment(ref.filename, ref.size_label, got.content, got.sha256,
                                        lambda sha, name, content: "not-stored", 20_000_000)
    texts = sum(1 for r in records if r.text)
    print(f"4. downloaded {ref.filename}: {len(got.content)} bytes; {len(records)} records, "
          f"{texts} with text; warnings: {warnings or 'none'}")


_MAGIC = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpeg", b"GIF8": "gif", b"RIFF": "webp?"}


def _shape(url: str) -> str:
    """The address without its file and date names and without any query: the pattern only
    (the module promises counts and names, never full addresses)."""
    parts = urlsplit(url)
    path = "/".join("<date>" if p.isdigit() and len(p) == 8 else p for p in parts.path.split("/")[:-1])
    ext = parts.path.rsplit(".", 1)[-1][:5] if "." in parts.path.rsplit("/", 1)[-1] else ""
    return f"{parts.scheme}://{parts.netloc}{path}/<name>{'.' + ext if ext else ''}" + (" (has a query)" if parts.query else "")


def _fetch(label: str, fetcher: SafeFetcher, url: str) -> str | None:
    """One fetch, reported; returns the bytes' sha256, or None when it failed or was no image."""
    try:
        got = fetcher.get(url)
    except Exception as exc:  # noqa: BLE001 - report and go on
        print(f"4. {label}: FAILED {type(exc).__name__}: {exc}")
        return None
    kind = next((v for k, v in _MAGIC.items() if got.content.startswith(k)), "not an image")
    sha = hashlib.sha256(got.content).hexdigest()
    print(f"4. {label}: status {got.status}, final path {_shape(got.url)}, type {got.headers.get('content-type')}, "
          f"{len(got.content)} bytes, bytes look like {kind}, sha {sha[:12]}")
    return sha if kind != "not an image" else None


def check_images(adapter: RbayAdapter, post, session) -> bool:
    """Read-only: how the post's text links its images (found the same way the scan reads the
    body), and whether the first R-BAY image is served the same twice with the session and once
    without its cookies (same headers). Images on other hosts are never fetched. Prints address
    patterns and sizes only. False when the body, an image or a fetch is missing."""
    page = adapter.post_page(post)
    table = BeautifulSoup(page.text, "html.parser").select_one("div.write_table table")
    body = _fields(table).get("내용") if table is not None else None
    editor = body.select_one(".editor_read") if body is not None else None
    images = (editor or body).select("img") if body is not None else []
    print(f"3. post {post.post_id}: post table found: {table is not None}; body found: {body is not None}; "
          f".editor_read found: {editor is not None}; images: {len(images)}")
    for img in images[:5]:
        src = img.get("src") or ""
        print(f"   img: {'embedded data: image' if src.startswith('data:') else _shape(urljoin(page.url, src))}")
    urls = [urljoin(page.url, i.get("src")) for i in images if i.get("src") and not i.get("src").startswith("data:")]
    own = [u for u in urls if urlsplit(u).hostname == HOST]
    if len(own) < len(urls):
        hosts = sorted({urlsplit(u).hostname or "?" for u in urls if u not in own})
        print(f"   images on other hosts (not fetched): {', '.join(hosts)}")
    if body is None or not own:
        print("FAILED: no R-BAY image found in the post's text")
        return False
    url = own[0]
    with closing(SafeFetcher(cookies=session.jar(), headers=session.headers(), verify=rbay_ssl_context())) as fetcher:
        first = _fetch("with session", fetcher, url)
        second = _fetch("with session again", fetcher, url)  # same fetcher: the delay applies
    with closing(SafeFetcher(headers=session.headers(), verify=rbay_ssl_context())) as plain:
        _fetch("without session cookies (same headers)", plain, url)
    if first is None or second is None:
        print("5. same bytes on both session fetches: not compared (a session fetch failed or was no image)")
        return False
    print(f"5. same bytes on both session fetches: {first == second}")
    return True


if __name__ == "__main__":
    sys.exit(main())
