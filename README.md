# IRB Watch

**English** | [한국어](README.ko.md)

A server that watches hospital IRB (institutional review board, the research ethics office) web boards for clinical research coordinators. Every day it reads the boards, notices which documents are new, changed or removed, keeps every version with its original files, and writes news cards with an AI summary and a priority. It tells people by email when something needs attention. Coordinators no longer have to open each hospital site and compare versions by hand.

On top of that it helps with the coordinator's own IRB work: a read-only assistant chat, AI drafts of submission and amendment forms (in a hospital's own blank form if there is one), and an **IRB pipeline** per study and hospital that records each step (initial review, amendments, continuing review, closure), its status, dates and submitted documents.

| | |
|---|---|
| **This repository** | the **server** (FastAPI) and the **database schema** (Supabase migrations) |
| **Frontend repository** | [pjh5474/irb-watch-frontend](https://github.com/pjh5474/irb-watch-frontend), the Next.js web app. It reads the same Supabase database directly under row-level security and calls this server for actions |
| **Running app** | the web app: <https://irb-watch-frontend.vercel.app> · the API: <https://irb-api.warwarsn.online> (OpenAPI description at `/openapi.json`) |

> **Status (2026-11-11).** Built and tested:
> - scanning with version history, AI summaries, members-only study boards, SNUH and R-BAY boards, and manual sources;
> - a daily scheduler (06:00 Asia/Seoul), the admin API, source health signals, and email;
> - the assistant chat, the docs generator with guides and form templates, and admin-edited AI instructions;
> - organizations with monthly usage, a users admin page, and password change;
> - the IRB pipeline (admin setup and users' pipelines).
>
> Deployed on one AWS server with automatic deploys; all 31 migrations are applied. **Much of the newest work is tested offline only:** the form templates, organizations, the users page and the pipeline have not been tried in a browser or against the real model. **AI drafts inside pipeline steps and due-date reminders are not built yet.** See [docs/plan.md](docs/plan.md) for what is open.

## How it works

```
admin adds a board URL (a public board, or a members-only "study" with its own stored account)
                                   │
        every day 06:00 (and on demand): read the board's list and every post page,
        download the attachments
                                   │
   original files ──► private Supabase Storage bucket (stored once per file hash)
   page text and file text (PDF, HWP, HWPX, DOCX) ──► extracted, normalized, hashed
                                   │
   compare with the stored state ──► new / updated / removed (after 2 missed scans) / reappeared
                                   │
   Jev (classifier): priority, substantive or cosmetic, category
   OpenAI (summary): a few bullets, in the document's language   (off for a source an admin keeps AI off for)
                                   │
   every version is stored with its full text ──► news card written to the audit log
                                   │
   source health checked ──► email alarms · daily report · user digest · weekly report
```

- **Version history.** Each change creates a version holding the full page text and each file's extracted text, so any two versions can be compared as a word-level diff.
- **The first scan of a board is a silent baseline.** It stores the posts and writes no cards.
- **Failures are visible.** A post that cannot be read, or a corrupt or password-protected attachment, is flagged `needs_review` with the reason. A failed scan changes nothing and never looks like "no changes". A rejected login pauses that source's scans so the hospital account is not locked.
- **Access.** Accounts are created by an admin. Signed-in users read all public sources. A **members-only source (a study)** is readable only by admins and by the users an admin assigned; a user who is not assigned does not even see that it exists. All writes go through this server.
- **AI is optional.** Only the changes (or, for an on-demand summary, the document text) are sent out, nothing is stored on OpenAI's side, and an admin can switch AI off per source (it starts off for studies, whose documents may be confidential). If a key is missing or a service is down, scans still work.
- **Summaries.** Users can ask for a whole-document summary, written as a short README (headings, bullets, a table of dates), cached after the first request and limited per user per day. Admins can write it again.
- **Email (Resend).** Admins get an alarm when a source turns amber or red, a recovery notice, the daily scan result and a weekly usage report. Users get one daily digest of new and updated documents with one-click unsubscribe. Studies appear only as "members-only source #id" and a count, never with names or titles. Mail is plain server code; no AI model writes or sends it.
- **Downloads.** Original files are handed out as 60-second signed links, each logged.
- **Assistant.** A read-only chat per user over the documents they may read, with daily limits set by admins.
- **Docs generator.** Drafts of IRB submission and amendment forms from stored document versions and per-hospital guides. You can work with the draft three ways:
  - edit it as Markdown, ask the AI to revise it, and export a DOCX;
  - with a hospital's own blank `.docx` form, have the AI answer field by field and export that form filled in;
  - admins can add instructions after the fixed safety rules.
- **IRB pipeline.** Inside a user's project, one pipeline per hospital, copied from an admin-set step order (global or per hospital):
  - steps with a status, due, submitted and decision dates, and a note;
  - documents uploaded per step, versioned by name, with a required-documents checklist;
  - a history of every change.

  Only the owner sees it. Removed things are kept 30 days. Per-step AI drafts are next.
- **Organizations.** Each user belongs to one organization. Organizations exist for registering users and for monthly usage statistics; they never grant access. Members-only sources are still opened to each user by an admin.

Boards supported today: the SNUH HRPP boards (`hrpp.snuh.org`), hospital e-IRBs on R-BAY (`r-bay.co.kr`), and manual sources an admin keeps up to date by upload. A new site needs a small adapter (see [docs/decisions.md](docs/decisions.md)).

## Requirements

- [uv](https://docs.astral.sh/uv/) (installs the pinned Python 3.12 for you)
- Node.js (the Supabase CLI runs through `npx`)
- A Supabase project (database, auth, storage)
- Optional: an OpenAI API key and model name (summaries), a TypeSafe (Jev) API key (classification), a Resend account with a verified sending domain (email)

## Setup (local)

Commands are PowerShell, run from the repository root.

```powershell
# 1. Install dependencies
uv sync

# 2. Create your settings file. APP_ENV picks the file: dev reads .env.dev, prod reads .env.prod.
Copy-Item .env.example .env.dev
# Fill in .env.dev. Never commit it (.gitignore excludes .env.*). Never paste its values into chats.
```

The variables (names only; every one is explained in `.env.example`):

| Variable | Needed for |
|---|---|
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | everything (the project's service or secret key; it stays on the server) |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | AI summaries (empty = off; there is no default model). `OPENAI_BASE_URL` is for an OpenAI-compatible gateway; leave it empty for OpenAI direct |
| `TYPESAFE_API_KEY`, `TYPESAFE_MODEL` | Jev classification (empty key = off) |
| `FRONTEND_ORIGINS` | browser origins allowed to call the API, comma-separated, no trailing slash (default `http://localhost:3000`) |
| `SCHEDULER_ENABLED`, `SCAN_CRON`, `SCAN_TIMEZONE` | the daily scan (off unless `SCHEDULER_ENABLED=true`; default `0 6 * * *` in `Asia/Seoul`) |
| `CREDENTIAL_STORE`, `CREDENTIALS_KEY` | where the hospital logins for members-only boards are kept (`keyring` on a Windows PC, `database` = encrypted table on a server; the key is yours and stays in env vars) |
| `RESEND_API_KEY`, `MAIL_FROM`, `APP_URL` | email. Mail is on only when all three are set |
| `UNSUBSCRIBE_KEY`, `API_DOMAIN` | the users' daily digest (signs the unsubscribe links; the API's public hostname) |
| `WEEKLY_STATS_CRON`, `ALERT_STALE_HOURS` | the weekly report time (write the weekday as a name, for example `0 7 * * mon`) and when a source counts as out of date (default 36 hours) |
| `AI_SKIP_BELOW`, `AI_MIN_CONFIDENCE`, `SUMMARY_REQUESTS_PER_DAY`, `DOWNLOAD_LINKS_PER_DAY` | tuning and per-user limits |

The server verifies login tokens against the project's public signing keys, so no JWT secret is needed.

```powershell
# 3. Create the database schema (the migrations are the source of truth)
npx supabase login
npx supabase link --project-ref <your-project-ref>
npx supabase db push --remote --dry-run      # preview first
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

Then run the [frontend](https://github.com/pjh5474/irb-watch-frontend) (`npm run dev`), sign in as the admin, and add a board on the admin Sources page (or with `POST /admin/sources`, then `POST /admin/sources/{id}/scan`). A scan takes about 2 to 3 minutes per board.

## Tests

```powershell
uv run pytest -q                  # offline, about half a minute
uv run ruff check irb_watch tests
```

Live tests that talk to the real Supabase project and a real hospital site run only when asked, create throwaway data and remove it afterwards (audit-log rows cannot be removed):

```powershell
$env:RUN_LIVE=1; uv run pytest -m live -q
```

## Deployment

One Ubuntu server (AWS EC2) runs Docker Compose with the API (headless Chromium for member logins, the scheduler) and Caddy for HTTPS. **Pushing to `main` runs the tests and then deploys** through GitHub Actions and a locked SSH key. Settings can live in **AWS Systems Manager Parameter Store** and be reloaded with a "refresh settings" workflow. Step by step, with the IAM policy and the email setup: [docs/deploy-ec2.md](docs/deploy-ec2.md). Database migrations are applied by hand (dry-run first); they are not part of the automatic deploy.

## Project layout

```
irb_watch/
  main.py            FastAPI app, CORS, error handling, scheduler start
  config.py          settings from the environment (APP_ENV picks the file)
  scheduler.py       APScheduler: daily scan, startup catch-up, retries, weekly report
  api/               routes: health, admin (users, organizations, sources, manual sources, members,
                     scans, credentials, mail, guides, form templates, AI instructions, pipeline
                     setup), document summaries, the assistant, the docs generator, pipelines,
                     file downloads, public unsubscribe
  auth/              login-token check against the project's public keys
  services/          business rules: users, organizations, sources, members, scans, runner,
                     summaries, downloads, alerts, digest, weekly report, assistant, docs
                     generator, pipeline setup, pipelines
  core/              change detection, diffs, hashing, source health, weekly and monthly figures:
                     pure logic
  crawlers/          polite fetcher (robots.txt, delays, SSRF guard), SNUH and R-BAY adapters and
                     readers, logins, zip unpacking
  parsers/           PDF, HWP, HWPX and DOCX text readers
  docgen/            draft prompts, the DOCX builder, form-template field detection and filling
  ai/                Jev classifier, OpenAI summarizer and assistant, skip rules, templates
  mail/              Resend client, mail templates (Korean first), unsubscribe tokens
  db/                Supabase implementations of the repositories
  security/          URL safety (no internal addresses), size limits, encryption of stored logins
  scripts/           one-off tools (first admin)
supabase/migrations/ the database schema
scripts/             deploy.sh and load-env.sh (run on the server)
tests/               offline tests with in-memory fakes; live tests behind RUN_LIVE=1
docs/                plan, decisions with reasons, runbook, deployment guide, daily reports
```

Design rules worth knowing before changing things:

- Business logic is written against small interfaces; `db/` implements them for Supabase and tests use in-memory fakes.
- The `audit_logs` table is append-only. A news card cannot be edited after it is written, so summaries are decided before the card is written.
- Migrations are never edited after they are applied; add a new one. New tables must `revoke all` before granting, because the project's default privileges are generous.
- Page and file text is untrusted input: data in prompts, size-capped, escaped in mail. No model call has tools that act in the world, and no model is given an email tool.
- Secrets live only in env files, the server's Parameter Store, or the owner's terminal. They are never committed, logged or printed.

Why each choice was made is in [docs/decisions.md](docs/decisions.md); commands are in [docs/runbook.md](docs/runbook.md); day-by-day progress is in [docs/reports/](docs/reports/).

## License

All rights reserved. No license is granted yet. The test fixtures include two public forms from a hospital website.
