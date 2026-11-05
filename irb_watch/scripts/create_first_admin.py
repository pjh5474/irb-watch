"""Create the very first admin account (sign-up is disabled, so the API has no admin yet).

    uv run python -m irb_watch.scripts.create_first_admin --email you@example.com
    APP_ENV=prod uv run python -m irb_watch.scripts.create_first_admin --email you@example.com

Reads Supabase settings from .env.<APP_ENV> (default dev). The password is asked for
interactively and never passed on the command line. Refuses to run if any active admin
already exists; use the admin API after that.
"""

import argparse
import getpass
import sys

from irb_watch.config import get_settings
from irb_watch.db.client import get_supabase_client
from irb_watch.db.supabase_users import SupabaseAuditSink, SupabaseAuthAdmin, SupabaseUserStore
from irb_watch.services.users import UserService, UserServiceError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the first admin user.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--display-name", default=None)
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        print(f"SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing (APP_ENV={settings.app_env})")
        return 1

    client = get_supabase_client()
    store = SupabaseUserStore(client)
    if store.count_active_admins() > 0:
        print("An active admin already exists. Use the admin API to create more users.")
        return 1

    print(f"Project: {settings.supabase_url}  (APP_ENV={settings.app_env})")
    password = getpass.getpass("Temporary password: ")
    if password != getpass.getpass("Repeat password: "):
        print("Passwords do not match.")
        return 1

    service = UserService(
        store, SupabaseAuthAdmin(client), SupabaseAuditSink(client), settings.min_password_length
    )
    try:
        profile = service.create_user(
            None, args.email, password, args.display_name, role="admin"
        )
    except UserServiceError as exc:
        print(f"Failed: {exc.message}")
        return 1

    print(f"Created admin {profile.email} (id {profile.id}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
