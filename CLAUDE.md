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
  services/          business rules: users, sources, scan, summaries (+ errors.py)
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
- **Untrusted input:** page and file text is untrusted. It is wrapped as data in prompts, only the changes are sent to OpenAI/Jev, and requests use `store=False`. Do not add tools (email, writes) to any model call without a human confirmation step.
- **Invisible characters:** do not write `​`-style escapes with the edit tools; they get converted into literal invisible characters. Use `chr()` or code points (see `core/hashing.py`).
- **No model name is hardcoded.** `OPENAI_MODEL` comes from the env file; without a key or model, AI parts switch off and scans still work.
- Keep `docs/decisions.md` and `docs/plan.md` current when a decision changes.

## Where things stand (2026-11-06)
Working and verified live: login and admin API, sources, manual scans of two SNUH boards (data stored in the project: sources 4 and 5), PDF/HWP/HWPX/DOCX text, change detection with version history, AI summaries (OpenAI `gpt-6-luna` + Jev), on-demand whole-document summaries.

Next: **the scheduler** (daily scan of every active source). Open questions to ask the owner first: run time and timezone, what to do about a run missed while the server was down, how many boards at once (a board takes about 2 minutes), and what to do if a scan is still running at the next run time.

Then: check Jev's accuracy against about 20 hand-labelled real revisions; verify row-level security and the on-demand endpoint with a real user's login token (needs the project's publishable key); real DOCX, HWPX and protected HWP samples; later, search over stored text and a read-only chat (email drafting only with a human send button).
