# Runbook

Commands you will repeat. Decisions and reasons are in `docs/decisions.md`.

## Settings files
`APP_ENV` picks the file: `APP_ENV=dev` (default) reads `.env.dev`, `APP_ENV=prod` reads `.env.prod`. Real environment variables override the file. Never commit these files (`.gitignore` blocks `.env.*` except `.env.example`).

Required for the server: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (service or secret key; server only). No JWT secret is needed: the project signs logins with ES256 keys and the server checks them against the public key list.

## Create the first admin (one-off script)
Public sign-up is disabled, so the API has no admin until you create one. Run once per Supabase project:

```powershell
uv run python -m irb_watch.scripts.create_first_admin --email you@example.com --display-name "Your Name"
```

- It asks for a temporary password (typed, not echoed, not stored in shell history). Minimum length is `MIN_PASSWORD_LENGTH` (default 8).
- It prints the project URL and `APP_ENV` before asking, so check you are on the project you mean.
- It refuses to run if an active admin already exists. After that, create users through `POST /admin/users`.
- It writes an `admin / user.created` entry to `audit_logs` with no actor.

### Moving to the real (production) Supabase project
1. Create the new project; turn off "Allow new users to sign up" in its Authentication settings.
2. Create `.env.prod` with that project's `SUPABASE_URL` and secret key.
3. `npx supabase link --project-ref <new ref>` then `npx supabase db push --remote --dry-run`, then `--remote --yes` to rebuild the whole schema from `supabase/migrations/`.
4. `$env:APP_ENV="prod"; uv run python -m irb_watch.scripts.create_first_admin --email ...`
5. Data and files from the demo project do not carry over.

## Admin API (all need `Authorization: Bearer <login token of an admin>`)
| Method and path | Purpose |
|---|---|
| `POST /admin/users` | Create user: `email`, `password` (temporary), optional `display_name`, `role`. Email is auto-confirmed |
| `GET /admin/users?limit=&offset=&include_deleted=` | List users, newest first. Deleted users hidden unless asked |
| `PATCH /admin/users/{id}/role` | `{"role": "user" or "admin"}` |
| `DELETE /admin/users/{id}` | Soft delete: sets `deleted_at` and bans the login. Reversible |
| `POST /admin/users/{id}/restore` | Clears `deleted_at` and lifts the ban |

Refused with 409: demoting or deleting yourself, removing the last active admin, creating a user whose email belongs to a deleted user (restore them instead).

Role changes are logged by a database trigger without an actor (the server uses the service role). Create, delete and restore are logged by the server with the acting admin.

## Tests
- `uv run pytest` runs offline (nothing touches Supabase).
- Live test against the real project (creates and removes two throwaway users; leaves audit rows because the log is append-only):
  `$env:RUN_LIVE=1; uv run pytest -m live`
- `uv run ruff check irb_watch tests`

## Supabase CLI (v3: commands are local by default, hosted needs `--remote`)
- New migration: `npx supabase migration new <name>`, then edit the file. Never edit an applied migration.
- Preview / apply: `npx supabase db push --remote --dry-run` / `npx supabase db push --remote --yes`
- Read-only check: `npx supabase db query --remote --read-only "<sql>"`
- Use `has_table_privilege()` to inspect grants; `information_schema` grant views are empty for the CLI's login role.
- The project's default privileges give `service_role` every privilege on new tables: always `revoke all`, then grant exactly what is needed.
