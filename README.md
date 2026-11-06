# IRB Watch

**English** | [한국어](README.ko.md)

A server that watches hospital IRB (institutional review board, the research ethics office) web boards for clinical research coordinators. It reads the boards on request, notices which documents are new, changed or removed, keeps every version with its original files, and writes news-card entries with an AI summary and a priority, so coordinators do not have to check each hospital site and compare versions by hand.

This repository holds the **server** and the **database schema**. The web dashboard is a separate Next.js project that reads the same Supabase database directly, under row-level security.

> Status: scanning, history, summaries and the admin API work and have been tested against a real hospital site. Scans start by hand; the daily scheduler and the dashboard are not built yet. See [docs/plan.md](docs/plan.md).

## How it works

```
admin registers a board URL ──► scan (manual for now)
                                   │
   read the board's list and every post page, download the attachments
                                   │
   original files ──► private Supabase Storage bucket (stored once per file hash)
   page text and file text (PDF, HWP, HWPX, DOCX) ──► extracted, normalized, hashed
                                   │
   compare with the stored state ──► new / updated / removed (after 2 missed scans) / reappeared
                                   │
   Jev (classifier): priority, substantive or cosmetic, category
   OpenAI (summary): 1-3 sentences, in the document's language
                                   │
   every version is stored with its full text ──► news card written to the audit log
```

- **Version history.** Each change creates a new version holding the full page text and each file's extracted text, so any two versions can be compared (by version number or by date) as a word-level diff.
- **The first scan of a board is a silent baseline.** It stores the current posts and writes no cards.
- **Failures are visible.** If a post cannot be read, or an attachment is corrupt or password-protected, the document is flagged `needs_review` with the reason, and subscribers see a card that says why. A failed scan changes nothing and never looks like "no changes".
- **AI is optional and cheap by design.** Title-only and clearly cosmetic changes get a plain template line without calling OpenAI. Only the changes (not whole documents) are sent out, and nothing is stored on OpenAI's side. If a key is missing or a service is down, scans still work.
- **Users can ask for a summary of a whole document** (`POST /documents/{id}/summary`): cached after the first request, and limited per user per day.
- **Access.** Accounts are created by an admin only. Signed-in users read the data they subscribed to through Supabase row-level security; all writes go through this server.

Boards supported today: the SNUH HRPP boards (`hrpp.snuh.org`). A new site needs a small adapter (see [docs/decisions.md](docs/decisions.md)).

## Requirements

- [uv](https://docs.astral.sh/uv/) (installs the pinned Python 3.12 for you)
- Node.js (the Supabase CLI runs through `npx`)
- A Supabase project (database, auth, storage)
- An OpenAI API key and a model name, for summaries (optional)
- A TypeSafe (Jev) API key, for classification (optional)

## Setup

Commands are PowerShell, run from the repository root.

```powershell
# 1. Install dependencies
uv sync

# 2. Create your settings file. APP_ENV picks the file: dev reads .env.dev, prod reads .env.prod.
Copy-Item .env.example .env.dev
# Fill in .env.dev. Never commit it (the .gitignore already excludes .env.*).
```

The variables (names only):

| Variable | Needed for |
|---|---|
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | everything (use the project's service or secret key; it stays on the server) |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | AI summaries (empty = off; there is no default model). `OPENAI_BASE_URL` is for an OpenAI-compatible gateway; leave empty for OpenAI direct |
| `TYPESAFE_API_KEY`, `TYPESAFE_MODEL` | Jev classification (empty key = off; the default model is `jev-latest`) |
| `FRONTEND_ORIGINS` | browser origins allowed to call the API (default `http://localhost:3000`) |
| `AI_SKIP_BELOW`, `AI_MIN_CONFIDENCE`, `SUMMARY_REQUESTS_PER_DAY` | tuning of the AI rules |

The server verifies login tokens against the project's public signing keys, so no JWT secret is needed.

```powershell
# 3. Create the database schema (the migrations are the source of truth)
npx supabase login
npx supabase link --project-ref <your-project-ref>
npx supabase db push --remote --dry-run      # preview
npx supabase db push --remote --yes          # apply
```

In the Supabase dashboard, turn off **Authentication → Allow new users to sign up**: accounts are created by an admin.

```powershell
# 4. Create the first admin (asks for a password; run it in your own terminal)
uv run python -m irb_watch.scripts.create_first_admin --email you@example.com --display-name "Your Name"

# 5. Run the server
uv run uvicorn irb_watch.main:app --reload
# GET http://127.0.0.1:8000/health  ->  {"status":"ok"}
```

Then register a board and start a scan with the admin API (needs the admin's login token): `POST /admin/sources` with a name and a board URL, then `POST /admin/sources/{id}/scan`. A scan takes about 2-3 minutes per board. All endpoints, statuses and commands are in [docs/runbook.md](docs/runbook.md).

### Tests

```powershell
uv run pytest -q                  # offline, about 10 seconds
uv run ruff check irb_watch tests
```

Live tests that talk to the real Supabase project and a real hospital site run only when asked, create throwaway data and remove it afterwards (audit-log rows cannot be removed):

```powershell
$env:RUN_LIVE=1; uv run pytest -m live -q
```

## Project layout

```
irb_watch/
  main.py            FastAPI app, CORS, error handling
  config.py          settings from the environment file
  api/               routes: health, admin users, admin sources, document summaries
  auth/              login-token check
  services/          business rules: users, sources, scans, summaries
  core/              change detection, diffs, hashing: pure logic, no network or database
  crawlers/          polite fetcher (robots.txt, delays, redirect checks), SNUH adapter and reader
  parsers/           PDF, HWP, HWPX and DOCX text readers
  ai/                Jev classifier, OpenAI summarizer, skip rules, templates
  db/                Supabase implementations of the repositories
  security/          URL safety (no internal addresses), size limits
  scripts/           one-off tools (first admin)
supabase/migrations/ the database schema
tests/               offline tests with in-memory fakes; live tests behind RUN_LIVE=1
docs/                plan, decisions with reasons, runbook, daily reports
```

Design rules worth knowing before changing things:

- Business logic is written against small interfaces; `db/` implements them for Supabase and tests use in-memory fakes.
- The `audit_logs` table is append-only. A news card cannot be edited after it is written, so summaries are decided before the card is written.
- Migrations are never edited after they are applied; add a new one. New tables must `revoke all` before granting, because the project's default privileges are generous.
- Page and file text is untrusted input. It is treated as data in prompts and size-capped; no model call has tools that act in the world.

Why each choice was made is in [docs/decisions.md](docs/decisions.md); day-by-day progress is in [docs/reports/](docs/reports/).

## License

All rights reserved. No license is granted yet. The test fixtures include two public forms from a hospital website.
