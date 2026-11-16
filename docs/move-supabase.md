# Moving to another Supabase project (2026-11-16: Mumbai -> Seoul)

Why: the old project `jlkkoaugtovvaxrqxoyx` is in `ap-south-1` (Mumbai) while the server is in
`ap-northeast-2` (Seoul). Every database call took 0.2-0.5 s, a step page 19 calls = 5.5 s
(measured on the server, 2026-11-16). The new project `qhtjighvtbvvxswyklae` ("Mira", company
account) is in Seoul.

The old project is never changed by these steps. Until step 3, going back is
`docker compose start api`.

## 1. Schema (done 2026-11-16)
All 41 migrations applied to the new project with `npx supabase db push --remote --yes` after
`npx supabase link --project-ref qhtjighvtbvvxswyklae`. Checked read-only: 48 tables, RLS on
everywhere, no anon access, 3 buckets, seed rows present.

## 2. Data and files (on the server)

Needed, typed by the owner only (never pasted into chats):
- both **Session pooler** connection URIs (dashboard -> Connect), with each database password.
  **A password with `@`, `#`, `/`, `:` or `%` breaks the URI:** reset that database password to
  letters and digits first (dashboard -> Database -> Settings), or percent-encode it;
- both projects' **server keys**, of the same kind the server uses today: a key starting
  `eyJ` is the legacy `service_role` key (Settings -> API Keys -> Legacy API keys), one starting
  `sb_secret_` is a secret key. Use the same kind for the new project.

**Pushing to `main` deploys** (`.github/workflows/deploy.yml` restarts the API on the server).
Push these scripts, wait for that deploy to finish, and push nothing more until step 3:
a deploy in the window would start the API (and the scheduler) against the old project.

### 2a. Files first, with the API still running (may take long; no downtime)
```bash
cd ~/irb-watch && git pull                     # brings scripts/move/ (already there after the deploy)
read -rs OLD_SERVICE_KEY; echo; read -rs NEW_SERVICE_KEY; echo
export OLD_SERVICE_KEY NEW_SERVICE_KEY
export OLD_SUPABASE_URL=https://jlkkoaugtovvaxrqxoyx.supabase.co
export NEW_SUPABASE_URL=https://qhtjighvtbvvxswyklae.supabase.co
copyfiles() { docker compose run --rm --no-deps -v "$HOME/irb-watch/scripts/move:/s:ro" \
       -e OLD_SUPABASE_URL -e OLD_SERVICE_KEY -e NEW_SUPABASE_URL -e NEW_SERVICE_KEY \
       api /app/.venv/bin/python /s/copy_storage.py "$@"; }

copyfiles --check   # counts only
copyfiles           # copies; a file already in the new project is skipped, so it can be run again
```

### 2b. Database, with the API stopped (the downtime: a few minutes)
**Nobody uses the site from here to step 3:** the browser writes some tables directly, and
those writes would go to the old project.
```bash
mkdir -p ~/irb-move && chmod 700 ~/irb-move    # dump files: they hold password hashes
docker compose stop api                        # from here the API answers 502

read -rs OLD_DB; echo; read -rs NEW_DB; echo   # paste the old URI, Enter, then the new URI, Enter
export OLD_DB NEW_DB
pg() { docker run --rm -i -e OLD_DB -e NEW_DB -v "$HOME/irb-move:/w" \
       -v "$HOME/irb-watch/scripts/move:/s:ro" postgres:17 bash /s/copy_db.sh "$@"; }

pg dump    # reads the old project: public data, auth.users, auth.identities, row counts, access
pg try     # loads into the new project, checks sequences, compares counts and access, then ROLLBACK
pg load    # only when "try" said counts match and access is the same: the same, then COMMIT

copyfiles          # the files added since 2a
copyfiles --check  # must say "0 would be copied" for every bucket
```
`try` and `load` print "access ... is the same" or the differing lines (send those; they hold no
secrets). If `try` fails on the users (`auth.sql`), stop and report; do not try another import.

If anything fails before step 3: send the error lines (never the URIs or keys), then
`docker compose start api`; the old project is unchanged.

## 3. Switch (only after step 2 shows equal counts and 0 files left)
- Server (Parameter Store `/irb-watch/prod`, or `.env`): `SUPABASE_URL`,
  `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_PUBLISHABLE_KEY` -> the new project's values.
  **Keep `CREDENTIALS_KEY` as it is**: it decrypts the stored hospital logins.
  The API reads the new values only after a reload: run the `refresh-settings` workflow
  (GitHub Actions, Run workflow), or on the server `scripts/deploy.sh`. Check
  `docker compose logs --tail 50 api` shows it healthy.
- Vercel: `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` -> new values; redeploy.
- New project, Authentication: sign-ups off; Site URL and Redirect URLs as in the old project;
  custom SMTP if the old one had it.
- Everyone signs in again (the new project signs logins with its own keys); passwords stay.
- Clean up: `rm -rf ~/irb-move; unset OLD_DB NEW_DB OLD_SERVICE_KEY NEW_SERVICE_KEY`.

## 4. Check
Sign in; open a step page (expect well under 1 s); one manual scan; download one stored file
and one pipeline file. Keep the old project 1-2 weeks, then pause it.

The local CLI is now linked to the new project; the old one is in another account and answers
403 to the CLI.
