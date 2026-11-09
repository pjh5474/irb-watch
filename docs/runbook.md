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

On the AWS server the settings come from Parameter Store (`/irb-watch/prod/<NAME>`) into `.env`; see `docs/deploy-ec2.md`. To change one: edit the parameter in the AWS console, then run the "refresh settings" workflow in GitHub Actions (or `scripts/deploy.sh refresh` on the server). Names of all settings are in `.env.example` and in the README.

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

## A bearer token for the API docs page
Open `https://irb-api.warwarsn.online/docs`, click **Authorize**, type your email and password (the same as the web app; leave `client_id` and `client_secret` empty), click Authorize. The page then sends the token with every Try-it-out call, for about an hour.
- Behind it: `POST /auth/token`, a form (`username` = email, `password`) that signs in against Supabase and returns `{"access_token", "token_type": "bearer", "expires_in"}`. Scripts can call it too: `curl -d "username=EMAIL" --data-urlencode "password=PASSWORD" https://irb-api.warwarsn.online/auth/token` (type the password in your own terminal).
- Needs `SUPABASE_PUBLISHABLE_KEY` on the server (the project's public key); without it the answer is 503 naming the setting.
- Wrong guesses: 5 failures per 15 minutes per email (from any address) and 5 per address (for any email), then 429 with `Retry-After`. The count lives in the server process and resets on restart. Anyone can therefore block one email's token login for 15 minutes by guessing; the web app's own login is not affected.
- A correct login writes the audit entry `auth.token_issued` (user id only). Failures are not logged. Deleted accounts get 403 even with the right password.
- The token is a normal login token: it has the user's own access, nothing more.

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
| `POST /admin/sources` | `{"name": "...", "url": "https://hrpp.snuh.org/hrpp/bbs/list.do?key=<number>", "login_user": "<label, optional>", "ai_enabled": <optional>}`. The URL is checked, rewritten to a clean form and matched to an adapter. With a `login_user` label the source is a members-only **study** and AI starts off. 409 if the same URL and label are already registered, 422 for unsupported sites |
| `GET /admin/sources` | All sources with last scan time, status (`ok`, `needs_review`, `failed`), error text, `scan_in_progress`, `login_user`, `requires_login`, `scans_paused_reason`, `ai_enabled` |
| `PATCH /admin/sources/{id}` | Rename; set `is_active` (inactive sources are not scanned), `login_user` (`""` removes it), `ai_enabled` |
| `POST /admin/sources/{id}/scan` | Starts a scan in the background and returns 202. 409 if one is already running. Poll `GET /admin/sources` for the result |
| `POST /admin/scans/run-all` | Scans every active source now (what the 06:00 job does); 202 |
| `GET /admin/sources/{id}/members` | Users assigned to a members-only source |
| `PUT /admin/sources/{id}/members` | `{"email": "..."}` assigns a user (idempotent; 422 for a public source, 404 for an unknown or deleted user) |
| `DELETE /admin/sources/{id}/members/{user_id}` | Removes the user's access; 204 |
| `GET /admin/credentials` | Which hospital logins are stored (labels and dates only) |
| `PUT /admin/credentials/{owner_label}/{site}` | `{"login_id": "...", "password": "..."}`. Stored encrypted, never returned. For a SNUH board the site is `snuh-<board key>`; the label is the source's `login_user`. Over HTTPS only |
| `DELETE /admin/credentials/{owner_label}/{site}` | Deletes the stored login; 204 |
| `POST /admin/documents/{id}/summary` | Writes the whole-document summary again, replacing the saved one for everyone (admin only, no daily limit; the old length is logged) |
| `POST /admin/mail/test` | Sends one test email to the calling admin only |
| `POST /admin/mail/weekly-preview` | Sends the weekly report for the last 7 days to the calling admin only; takes no weekly claim, so Monday's real mail still goes out |

- A scan takes about 3 minutes per board (2-second delay between requests, plus file downloads).
- First scan of a source is a silent baseline: documents are stored, no "new" events.
- `needs_review` on a source means the site changed shape, the list was incomplete or a login was rejected: read `last_scan_error`, fix the cause, scan again. Nothing was changed by that scan. After a rejected login `scans_paused_reason` is set and daily scans skip that source so the hospital account is not locked; a manual scan that logs in successfully clears it.
- `needs_review` on a document means one post has a problem: read `documents.review_reason` (also shown to users as a `document.needs_review` news entry). Typical reasons: an attachment is password-protected or corrupt, or the post's page could not be read. The post stays tracked; fix or re-upload at the source, then scan again and the flag clears.
- Files with no text are not errors by themselves: `document_version_files.hash_only_reason` says why (`no_text_layer` scanned PDF, `distribution_protected` HWP, `unsupported_format`), and `extract_note` gives the words. Only `unreadable` raises a flag.
- A stuck `scan_in_progress` clears itself after 30 minutes.

## Studies (members-only sources)
1. Add the source with a `login_user` label (or `PATCH` the label onto an existing one). It is now readable only by admins and assigned users, and AI is off.
2. Store the study's hospital login: `PUT /admin/credentials/<label>/snuh-<board key>` (the admin screen does this). Without it scans fail with a clear reason.
3. Assign users with `PUT /admin/sources/{id}/members`.
4. Turn AI on only if the owner agrees the documents may go to OpenAI and Jev (`PATCH` `ai_enabled: true`).
5. The same board can be added again with a different label (different study account); the pair URL + label must be unique.

## Scheduler
- Runs only with `SCHEDULER_ENABLED=true`. Daily scan at `SCAN_CRON` (default 06:00 in `SCAN_TIMEZONE`, Asia/Seoul), one worker per hospital site, one retry after 1 hour, a catch-up run at start-up if today's was missed.
- Weekly admin report at `WEEKLY_STATS_CRON` (default `0 7 * * mon`, Mondays 07:00). Write the weekday as a **name**: APScheduler counts numbers from Monday = 0, so `1` means Tuesday.
- Look at what happened: `GET /admin/sources` (status per source), the audit log (`scan.run_finished`, `mail.sent`, `mail.failed`) and the server logs (`docker compose logs --tail 200 api` on the server).

## The assistant (chat agent)
- Needs `OPENAI_API_KEY` and `OPENAI_MODEL` (the same as summaries); without them every agent call answers 503 and nothing else changes.
- Routes (login token of any active user): `GET /agent/status` (enabled, used, limit), `POST /agent/conversations`, `GET /agent/conversations`, `GET /agent/conversations/{id}/messages`, `DELETE /agent/conversations/{id}`, `POST /agent/conversations/{id}/messages` (body `{"content": "...", "page": {"kind": "source|document|other", "id": 12}}`; answers as server-sent events `text`, `tool`, `action`, `done`, `error`). Another user's conversation is a 404.
- Limits: `GET` / `PUT /admin/agent/settings` with `{"user_daily_limit": 30, "admin_daily_limit": 100}` (0 to 1000; 0 switches the assistant off for that account type). Messages are counted per rolling 24 hours from the audit log (`agent.message`).
- Sources with AI switched off (`ai_enabled`) are invisible to the assistant; to let it use a study, switch AI on for that source (owner's decision per study).
- Conversations are deleted 30 days after their last message by the `chat-purge` job (03:30, only when the scheduler runs); audit entry `agent.purged` holds the count.
- Admin proposals (`action` events) do nothing by themselves: the browser runs the normal admin endpoint after the admin clicks Confirm.
- Look at what happened: audit entries `agent.message`, `agent.settings_changed`, `agent.purged`; server logs say `assistant answer failed: <ErrorType>` (never the document text).

## Email (Resend)
- On only when `RESEND_API_KEY`, `MAIL_FROM` and `APP_URL` are all set; otherwise mail steps are skipped and logged, scans are unaffected. The digest also needs `UNSUBSCRIBE_KEY` and `API_DOMAIN`.
- Admins get: an alarm when a source turns amber or red, a recovery notice, the daily scan result, and the weekly usage report. Users get one daily digest when something new or updated is in sources they can read; studies are a count only, never names or titles. Users switch it off in Settings or with the unsubscribe link (`GET /unsubscribe?token=` checks it and changes nothing; `POST /unsubscribe?token=` switches it off; no login).
- Each mail kind has a once-only key in `mail_claims` (a day or a week), so a restart or retry does not send twice; a failed send releases its key.
- Mail is plain server code. No model has an email tool.
- Check setup: `POST /admin/mail/test` (the "Send test email" button). A failure shows the mail service's own reason (for example an unverified domain).

## AI summaries
- Keys live in `.env.<APP_ENV>`: `OPENAI_API_KEY` + `OPENAI_MODEL` (summaries) and `TYPESAFE_API_KEY` (Jev). Leave one out to switch that part off; scans keep working. The server logs a warning at start-up for whichever is off.
- During a scan each new or updated post is classified (priority, category) and summarized; the result is saved on the version and shown on its news card (`audit_logs.summary`, `priority`, payload `category`, `summary_source`, `ai_note`). `summary_source` is `ai`, `template` (plain description without AI) or `none`.
- Change the categories or the priority wording in `irb_watch/ai/categories.py`; change when AI is skipped with `AI_SKIP_BELOW` and `AI_MIN_CONFIDENCE`.
- Users ask for a whole-document summary with `POST /documents/{id}/summary` (Authorization: Bearer <login token>). The summary is written as a short README (Markdown) and cached per version. 404 if the document does not exist or the user may not read its source, 429 after `SUMMARY_REQUESTS_PER_DAY` new summaries in 24 hours (cached ones do not count), 503 if AI is off for the server or for that source (`ai_enabled`), 502 if the summary service failed (the error type is in the message).
- Files: `POST /files/{id}/download-url` returns a 60-second signed link and logs it; limited by `DOWNLOAD_LINKS_PER_DAY`.
- Only the changes (or, for on-demand, the document text) are sent to OpenAI and Jev; nothing is stored on OpenAI's side (`store=False`).

## Tests
- `uv run pytest -q` runs offline (nothing touches Supabase).
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
