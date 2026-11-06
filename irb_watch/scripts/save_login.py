"""Log in to a members-only board once in a visible browser and save the session.

    uv run python -m irb_watch.scripts.save_login --user owner --site snuh-2605277031 \
        --url "https://hrpp.snuh.org/hrpp/bbs/list.do?key=2605277031"

A Chromium window opens. Log in yourself; this script never sees the password. When the
board is showing, come back to the terminal and press Enter. Cookies are saved to
data/sessions/<user>/<site>.json (gitignored). Treat that file like a password.
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from irb_watch.crawlers.sessions import FileSessionStore

SESSIONS_DIR = Path("data") / "sessions"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Save a browser login for one user and site.")
    parser.add_argument("--user", required=True, help="who owns this login, e.g. owner")
    parser.add_argument("--site", required=True, help="short name for the board, e.g. snuh-31")
    parser.add_argument("--url", required=True, help="https page to open first")
    args = parser.parse_args(argv)

    if not args.url.startswith("https://"):
        print("Only https URLs are allowed.")
        return 1

    store = FileSessionStore(SESSIONS_DIR)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(args.url)
        input("Log in in the browser window, open the board, then press Enter here... ")
        state = context.storage_state()
        browser.close()

    cookie_count = len(state.get("cookies", []))
    if cookie_count == 0:
        print("No cookies were captured; nothing saved.")
        return 1
    store.save(args.user, args.site, state)
    print(f"Saved {cookie_count} cookies for user '{args.user}', site '{args.site}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
