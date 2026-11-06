"""Store a site's ID and password in Windows Credential Manager (never in the repo).

    uv run python -m irb_watch.scripts.save_credentials --user owner --site snuh-2605277031

The ID is typed at a prompt and the password through getpass (hidden). Nothing is printed
back. Run it in your own terminal; the assistant must never see the password.
"""

import argparse
import getpass
import sys

from irb_watch.crawlers.credentials import Credentials, KeyringCredentialStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Save a site login for one user.")
    parser.add_argument("--user", required=True)
    parser.add_argument("--site", required=True)
    args = parser.parse_args(argv)

    login_id = input("Site login ID: ").strip()
    password = getpass.getpass("Site password (hidden): ")
    if not login_id or not password:
        print("ID and password are both required; nothing saved.")
        return 1

    KeyringCredentialStore().set(args.user, args.site, Credentials(login_id, password))
    print(f"Saved login for user '{args.user}', site '{args.site}' in Credential Manager.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
