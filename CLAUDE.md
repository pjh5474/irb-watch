# IRB Watch server

FastAPI server that scans hospital IRB (research ethics office) web boards on request, detects new/updated/removed documents, keeps full version history and the original files, and writes news-card entries with AI summaries. The frontend (Next.js) is a separate repo and reads Supabase directly under row-level security. This repo holds the server and the Supabase schema.

Read first: `docs/plan.md` (state and next steps), `docs/decisions.md` (every decision with its reason), `docs/runbook.md` (commands). Daily reports are in `docs/reports/`.

## How to work with me (the owner)
- **Ask before planning.** For a new feature, first ask about every decision that cannot be inferred from the repo, including edge cases that could go either way. Use short multiple-choice questions with a recommended option. Then build.
- **Wait for an explicit "go" before:** applying a migration to the Supabase project, running a live test that writes data, committing, or anything outward-facing. Always dry-run migrations first.
- **Edit files only with the Edit and Write tools, never through shell commands** (sed, echo >, Set-Content). The owner uses the rewind function, which only tracks those tools. Read a file before overwriting it; never overwrite blindly.
- **Never ask for, print or read secrets.** Do not open `.env.dev` / `.env.prod`. To check whether a key is set, print only a boolean. Passwords are typed by the owner in their own terminal.
- **Report honestly:** say what was verified, what was only tested with fakes, and what is unknown. Flag your own mistakes. Failures must always show a reason to users; a failure must never look like "no change".
- Replies use the Spartan output style (`.claude/output-styles/spartan.md`): answer first, short blocks, bold lead-ins.
- Commit messages end with the co-author line the harness provides. Stage with `git add -A`, then check that no `.env*` file except `.env.example` is staged and scan the diff for key-like strings before committing.

## Layout
```
irb_watch/
  main.py            FastAPI app, CORS, error handler
  config.py          settings; APP_ENV picks .env.<APP_ENV> (default dev)
  api/               routers (health, admin_users, admin_sources, documents) and deps.py
  auth/jwt.py        login-token check against the project's public ES256 keys
  services/          business rules: users, sources, scan, runner, access, summaries (+ errors.py)
  scheduler.py       APScheduler daily run + catch-up + retries (off unless SCHEDULER_ENABLED)
  core/              state, detect (changes), diff, hashing, models: pure logic, no I/O
  crawlers/          fetcher (SSRF guard, robots, delays), snuh adapter, snuh_reader
  parsers/           pdf, hwp, hwpx, docx readers; formats.py picks one; extract_text()
  ai/                Jev classifier, OpenAI summarizer, enrich (skip rules), digests, templates
  db/                Supabase-backed repositories (documents.py, supabase_users.py, client.py)
  security/          url_guard (SSRF), limits
  scripts/           create_first_admin.py
supabase/migrations/ the schema; the source of truth (Supabase CLI, no ORM)
tests/               offline tests; test_live_*.py hit the real project only when RUN_LIVE=1
```
Logic lives in `core/` and `services/` against small protocols; `db/` implements them for Supabase and tests use in-memory fakes (`tests/fakes*.py`).

## Commands (PowerShell, from the repo root)
```
uv run pytest -q                      # offline, about 10 s
uv run ruff check irb_watch tests     # must stay clean
$env:RUN_LIVE=1; uv run pytest -m live -k scan -q -s   # live scan test, about 6-10 min, writes then cleans up

npx supabase migration new <name>                 # then fill the file with Write
npx supabase db push --remote --dry-run           # preview
npx supabase db push --remote --yes               # apply (only after the owner says go)
npx supabase db query --remote --read-only "<sql>"   # verify; use has_table_privilege() for grants
```
Supabase CLI v3 targets the local stack by default; hosted needs `--remote`. There is no Docker, so no local stack. The CLI labels the one project "production"; it is the owner's test project.

## Rules that are easy to get wrong
- **Never edit an applied migration;** add a new one. The project's default privileges give `service_role` every privilege on new tables, so always `revoke all` first, then grant exactly what is needed. `audit_logs` is append-only: cards cannot be edited after writing, so summaries are decided before the card is written.
- **Failure model:** a whole-scan failure changes no documents and sets the source `needs_review` or `failed`; one broken post is flagged with a reason and the rest still processed; an unreadable attachment keeps the post tracked and is flagged; expected no-text files (scanned PDF, distribution HWP, legacy .doc) are hash-only with no alert. A post missing from a complete scan is removed only after 2 consecutive scans. The first scan of a source is a silent baseline (no cards).
- **Untrusted input:** page and file text is untrusted. It is wrapped as data in prompts (closing tags defused with `ai/tags.py`, any case or spacing) and requests use `store=False`. Scans send only the changes to OpenAI/Jev; whole documents are sent only by user actions (on-demand summary, the chat's document text, the docs generator), each capped. Sources with `ai_enabled = false` send nothing, except a docs-generator draft where the user ticked the box and admin test runs. Do not add tools (email, writes) to any model call without a human confirmation step.
- **Invisible characters:** do not write `​`-style escapes with the edit tools; they get converted into literal invisible characters. Use `chr()` or code points (see `core/hashing.py`).
- **No model name is hardcoded.** `OPENAI_MODEL` comes from the env file; without a key or model, AI parts switch off and scans still work.
- Keep `docs/decisions.md` and `docs/plan.md` current when a decision changes.

## Where things stand (2026-11-10)
**All 19 migrations are applied** (the 19th, `user_projects`, on 2026-11-10: private per-user folders of sites, see decisions.md "User projects") (checked with `npx supabase migration list --remote` on 2026-11-10). Each feature below has its backend and frontend built and committed. The frontend is the separate repo `irb-watch-frontend`.

Working and verified live: login and admin API, sources, manual scans of two SNUH boards (data stored in the project: sources 4 and 5), PDF/HWP/HWPX/DOCX text, change detection with version history, AI summaries (OpenAI `gpt-6-luna` + Jev), on-demand whole-document summaries, a Docker deploy to an AWS server (`docs/deploy-ec2.md`; `https://irb-api.warwarsn.online`).

Also built:
- **Scans:** login-only boards (a fresh headless login per scan, `requires_login`), atomic scan saving (`commit_scan`), 502 retry rules.
- **The scheduler:** runs at 06:00 Asia/Seoul, with a startup catch-up, one worker per site, one retry after 1 hour, and a pause after a rejected login. It is off until `SCHEDULER_ENABLED=true`. One live `run_all` over sources 4, 5 and 6 succeeded; the 06:00 trigger itself has not been confirmed.
- **Downloads and safety:** download links (`POST /files/{id}/download-url`), fail-closed audit entries, an encrypted credential table (`CREDENTIAL_STORE=database`).
- **Study sources (2026-11-09):** a members-only source is readable only by admins and by users an admin assigned in `source_members`. There are no subscriptions. AI is off for these sources by default (`sources.ai_enabled`). Admin routes: `/admin/sources/{id}/members` and `POST /admin/documents/{id}/summary`.
- **Email (2026-11-09):**
  - Phase 1, admin alarms and the daily report: a real test mail arrived.
  - Phase 2, the user digest with one-click unsubscribe: frontend built.
  - Phase 3, the weekly admin usage mail (Mondays 07:00 Seoul; `POST /admin/mail/weekly-preview`): a real send has not been confirmed. The cron weekday must be a name (`mon`); in APScheduler, numbers are off by one.
  - Mail is plain server code, never a model tool.
- **The assistant (2026-11-09):** a read-only chat per user that streams server-sent events. Admins set daily limits. Admins may get Confirm-button proposals, which the browser runs through the normal admin API. It has run against the real model. The stream through Caddy (`flush_interval -1` on the stream route) looked like streaming in the browser, but the `live_agent_check` count is still inconclusive. See decisions.md "The assistant (agent), backend".
- **Docs generator (2026-11-10):** drafts IRB submission and amendment forms from stored document versions plus per-source guides. Admins keep a guide library; a user may detach or attach guides. The draft is Markdown first, edited by hand or revised by the AI; a plain DOCX comes on demand. Saving an edit made from an older version is refused (409). An AI-off study is used only when the user ticks a box for that source, and each use is audit-logged. Backend: `irb_watch/docgen/`, `services/docgen.py`, `api/docgen.py`, `api/admin_guides.py`. The RLS test passed (rolled back). Not yet checked by the owner: draft quality with the real model, Word opening the hand-made DOCX, and a draft finishing after the user leaves the page.
- **Admin-edited AI instructions (2026-11-10):** admins add text after the fixed safety rules of the chat, the docs generator and summaries. The text has history, restore, reset and a test box, and takes effect without a restart. The safety core stays in code. Code: `ai/admin_text.py`, `services/ai_instructions.py`, `api/admin_ai.py`.

Next candidates:
- search over stored document text
- check Jev's accuracy against about 20 hand-labelled real revisions
- verify RLS and the endpoints with a real user's login token (needs the project's publishable key)
- real DOCX, HWPX and protected HWP samples
- later, a layout per document type in the docs generator (needs real hospital templates)
