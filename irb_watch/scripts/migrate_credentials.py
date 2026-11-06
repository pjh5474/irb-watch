"""Copy a hospital login from Windows Credential Manager into the encrypted database table.

    uv run python -m irb_watch.scripts.migrate_credentials --user owner --site snuh-2605277031
    APP_ENV=prod uv run python -m irb_watch.scripts.migrate_credentials --user owner --site snuh-2605277031

Run it in your own terminal. It needs SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY and
CREDENTIALS_KEY in the .env.<APP_ENV> file. The login is read from the keyring, encrypted
and written; nothing secret is printed. The keyring entry is left in place.
"""

import argparse
import sys

from irb_watch.config import get_settings
from irb_watch.crawlers.credentials import KeyringCredentialStore
from irb_watch.crawlers.errors import CredentialStoreError
from irb_watch.db.client import get_supabase_client
from irb_watch.db.credentials import DatabaseCredentialStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copy one stored login keyring -> database.")
    parser.add_argument("--user", required=True)
    parser.add_argument("--site", required=True)
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.credentials_key:
        print(f"CREDENTIALS_KEY is not set (APP_ENV={settings.app_env}); nothing copied.")
        return 1

    credentials = KeyringCredentialStore().get(args.user, args.site)
    if credentials is None:
        print(f"No keyring entry for user '{args.user}', site '{args.site}'; nothing copied.")
        return 1

    store = DatabaseCredentialStore(get_supabase_client(), settings.credentials_key)
    try:
        store.set(args.user, args.site, credentials)
        stored = store.get(args.user, args.site)  # proves the key round-trips
    except CredentialStoreError as exc:
        print(f"Could not store the login: {exc}")
        return 1
    if stored != credentials:
        print("The stored login does not match the keyring entry; please check the key.")
        return 1
    print(f"Copied and verified: user '{args.user}', site '{args.site}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
