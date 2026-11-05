# Runbook

Commands you will repeat. Decisions and reasons are in `docs/decisions.md`.

## Quick reference: create the first admin
Run this yourself in a normal terminal, from the project folder (it asks for a hidden password, so it cannot run through Claude):

```powershell
uv run python -m irb_watch.scripts.create_first_admin --email you@example.com --display-name "Your Name"
```

Production project: set `APP_ENV` first, so it reads `.env.prod` instead of `.env.dev`:

```powershell
$env:APP_ENV = "prod"
uv run python -m irb_watch.scripts.create_first_admin --email you@example.com --display-name "Your Name"
```

Check that the printed project URL is the one you mean before typing the password. Full notes below.

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

## Sources and scans (admin only)
| Method and path | Purpose |
|---|---|
| `POST /admin/sources` | `{"name": "...", "url": "https://hrpp.snuh.org/hrpp/bbs/list.do?key=<number>"}`. The URL is checked, rewritten to a clean form and matched to an adapter. 409 if already registered, 422 for unsupported sites |
| `GET /admin/sources` | All sources with last scan time, status (`ok`, `needs_review`, `failed`), error text and `scan_in_progress` |
| `PATCH /admin/sources/{id}` | Rename or set `is_active` (inactive sources cannot be scanned) |
| `POST /admin/sources/{id}/scan` | Starts a scan in the background and returns 202. 409 if one is already running. Poll `GET /admin/sources` for the result |

- A scan takes about 3 minutes per board (2-second delay between requests, plus file downloads).
- First scan of a source is a silent baseline: documents are stored, no "new" events.
- `needs_review` on a source means the site changed shape or the list was incomplete: read `last_scan_error`, fix the adapter, scan again. Nothing was changed by that scan.
- `needs_review` on a document means one post has a problem: read `documents.review_reason` (also shown to subscribers as a `document.needs_review` news entry). Typical reasons: an attachment is password-protected or corrupt, or the post's page could not be read. The post stays tracked; fix or re-upload at the source, then scan again and the flag clears.
- Files with no text are not errors by themselves: `document_version_files.hash_only_reason` says why (`no_text_layer` scanned PDF, `distribution_protected` HWP, `unsupported_format`), and `extract_note` gives the words. Only `unreadable` raises a flag.
- A stuck `scan_in_progress` clears itself after 30 minutes.

## AI summaries
- Keys live in `.env.<APP_ENV>`: `OPENAI_API_KEY` + `OPENAI_MODEL` (summaries) and `TYPESAFE_API_KEY` (Jev). Leave one out to switch that part off; scans keep working. The server logs a warning at start-up for whichever is off.
- During a scan each new or updated post is classified (priority, category) and summarized; the result is saved on the version and shown on its news card (`audit_logs.summary`, `priority`, payload `category`, `summary_source`, `ai_note`). `summary_source` is `ai`, `template` (plain description without AI) or `none`.
- Change the categories or the priority wording in `irb_watch/ai/categories.py`; change when AI is skipped with `AI_SKIP_BELOW` and `AI_MIN_CONFIDENCE`.
- Users ask for a whole-document summary with `POST /documents/{id}/summary` (Authorization: Bearer <login token>). 404 if they are not subscribed, 429 after `SUMMARY_REQUESTS_PER_DAY` new summaries in 24 hours, 503 if AI is off.
- Only the changes (or, for on-demand, the document text) are sent to OpenAI and Jev; nothing is stored on OpenAI's side (`store=False`).

## Tests
- `uv run pytest` runs offline (nothing touches Supabase).
- Live test against the real project (creates and removes two throwaway users; leaves audit rows because the log is append-only):
  `$env:RUN_LIVE=1; uv run pytest -m live`
- Live scan test (scans a real SNUH board into the real project, about 10 minutes, cleans up after itself except audit rows):
  `$env:RUN_LIVE=1; uv run pytest -m live -k scan`
- `uv run ruff check irb_watch tests`

## Supabase CLI (v3: commands are local by default, hosted needs `--remote`)
- New migration: `npx supabase migration new <name>`, then edit the file. Never edit an applied migration.
- Preview / apply: `npx supabase db push --remote --dry-run` / `npx supabase db push --remote --yes`
- Read-only check: `npx supabase db query --remote --read-only "<sql>"`
- Use `has_table_privilege()` to inspect grants; `information_schema` grant views are empty for the CLI's login role.
- The project's default privileges give `service_role` every privilege on new tables: always `revoke all`, then grant exactly what is needed.
