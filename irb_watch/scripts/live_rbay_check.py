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
import sys

from irb_watch.api.deps import get_credential_store
from irb_watch.crawlers.attachments import read_attachment
from irb_watch.crawlers.credentials import KeyringCredentialStore
from irb_watch.crawlers.fetcher import SafeFetcher
from irb_watch.crawlers.rbay import BASE_URL, RbayAdapter
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
    args = parser.parse_args(argv)

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
        with_files = None
        chosen = [p for p in posts if p.post_id == args.post] if args.post else posts[:10]
        if args.post and not chosen:
            print(f"3. post {args.post} is not on this board")
        for post in chosen:
            detail = adapter.fetch_post(post)
            if detail.attachments:
                with_files = detail
                break
        if with_files is None:
            print("3. no file in the first 10 posts; download not checked")
        else:
            ref = with_files.attachments[0]
            print(f"3. post {with_files.summary.post_id}: {len(with_files.body_text)} chars, "
                  f"{len(with_files.attachments)} files")
            got = adapter.download(ref)
            records, warnings = read_attachment(ref.filename, ref.size_label, got.content, got.sha256,
                                                lambda sha, name, content: "not-stored", 20_000_000)
            texts = sum(1 for r in records if r.text)
            print(f"4. downloaded {ref.filename}: {len(got.content)} bytes; {len(records)} records, "
                  f"{texts} with text; warnings: {warnings or 'none'}")
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


if __name__ == "__main__":
    sys.exit(main())
