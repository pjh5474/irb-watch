# IRB Watch server: structure plan

Decision history and reasons: `docs/decisions.md`.

## Context
This repo is the backend only: a FastAPI server that scans admin-registered IRB URLs on a schedule, detects new/updated/removed documents deterministically, and uses an OpenAI agent solely to summarize diffs and rank priority. Frontend (Next.js) lives in another repo and reads Supabase directly via supabase-js + RLS. Supabase schema/migrations live HERE.

## Target structure
```
irb-watch/
  pyproject.toml            uv-managed
  .env.example
  Dockerfile
  docs/                     decisions.md, plan.md
  supabase/
    config.toml
    migrations/             profiles (+ signup trigger), sources, documents,
                            document_versions, document_version_files,
                            subscriptions, audit_logs, private `documents`
                            bucket, soft delete, scan lock, RLS (5 applied)
  irb_watch/
    main.py                 FastAPI app + scheduler startup
    config.py               pydantic-settings (env)
    api/                    routers: health, admin_sources, admin_users, scans
    auth/                   Supabase JWT verify, require_admin dependency
    db/                     supabase client (service role), repositories
    scheduler/              APScheduler jobs, run lock
    security/               url_guard (SSRF), robots, size/time limits
    crawlers/               httpx fetcher, playwright fetcher, link extractor
    parsers/                pdf, html, (hwp later) -> text; failures raised, not guessed
    core/                   hash, diff, change classification, 2-miss removal logic
    ai/                     agent: summarize diff + priority (structured output)
  tests/
    fixtures/               saved pages/files with known changes
```
`notify/` was out of the MVP and has been deleted.

## Implementation steps
1. [done] `pyproject.toml` for uv, `.env.example`, `Dockerfile`.
2. [done] `config.py`, `main.py`, `/health`, `tests/test_health.py`. Python pinned to 3.12.
3. [done] Supabase migrations applied to project `jlkkoaugtovvaxrqxoyx` and verified with read-only queries: `profiles` + signup trigger, domain tables + audit log + private bucket, audit-log append-only fix. **Not yet exercised with real users:** RLS policies and the three triggers.
4. [done] JWT verification (`auth/jwt.py`, ES256/RS256 via the public key list, no secret), `require_admin` (role looked up in the DB on every call), admin API `/admin/users` (create, list, change role, soft delete, restore) with lock-out guards, `profiles.deleted_at` migration, first-admin script, `APP_ENV` settings loader, CORS. 108 offline tests plus a live lifecycle test (`RUN_LIVE=1`) that passed against the real project. Commands in `docs/runbook.md`. **Not yet verified:** RLS from a real user's token, refresh-token behaviour after a ban, a real login token against the verifier. **No admin exists yet:** run the first-admin script.
5. [done] `security/url_guard` + `limits` + tests.
6. [done] Scan pipeline, verified end to end by a live test (`tests/test_live_scan.py`, board 2605277029, about 10 minutes for 3 scans):
   - SNUH adapter (`crawlers/snuh.py`, `crawlers/fetcher.py`) and reader (`crawlers/snuh_reader.py`: one `Observation` per post, originals handed to a file sink, one broken post only fails itself).
   - PDF text parser (`parsers/`): text, hash-only (scanned, HWP, DOCX) or `ParseError`.
   - Change detection and diffs (`core/detect.py`, `core/diff.py`, `core/state.py`): baseline, new, updated, resaved, removed after 2 misses, reappeared, needs_review; versions picked by number or date.
   - `services/scan.py` (claim lock, whole-scan failure statuses, audit events) and `db/documents.py` (Supabase repository, bucket upload only for new hashes).
   - Migration `document_files_and_scan_lock` (files table with text, `new` and `resaved` change types, `documents.latest_version_id`, `sources.scan_started_at`).
   - Admin source API: `POST/GET /admin/sources`, `PATCH /admin/sources/{id}`, `POST /admin/sources/{id}/scan` (202, runs in the background).
   - Text readers for HWP, HWPX and DOCX (`parsers/hwp.py`, `hwpx.py`, `docx.py`, shared `common.py`, `_zip.py`, `_xmltext.py`); two real SNUH HWP forms saved as fixtures and cross-checked against the preview text stored inside each file; the live scan test now asserts HWP files come back with text. Failure reasons are stored and announced to subscribers (`review_reason`, `extract_note`, `document.needs_review`).
   - **Not yet verified:** second board `2605277034`; the new endpoints over real HTTP with an admin login; a real layout-change or network failure (covered offline only); real DOCX, HWPX, password-protected and distribution HWP files (covered by synthetic files and edited headers only); legacy `.doc` stays hash-only.
7. [done, verified live] AI step in `irb_watch/ai/`: Jev classifier (`jev.py`), OpenAI Responses API summarizer (`openai_summarizer.py`), `enrich.py` (skip rule, fallbacks, caps), template text (`templates.py`), digests (`digest.py`), editable questions (`categories.py`), built from settings in `factory.py`. Wired into `ScanService`; user-requested whole-document summaries in `services/summaries.py` and `POST /documents/{id}/summary`. Migration `ai_summaries_and_requests` adds category, summary_source, classification, document_summary and the `summary_requests` table. The migration is applied. The live scan test (`tests/test_live_scan.py`, about 6 minutes) now runs with OpenAI (`gpt-6-luna`) and Jev (`jev-1.14.0`) on: baseline makes no AI calls, a changed post gets an AI summary plus Jev priority and category, and a subscribed user gets a whole-document summary that is then served from the cache. That update was forced by altering stored text, so it proves the wiring, not summary quality. **Still open:** Jev's accuracy on real IRB revisions (label about 20 and compare), the on-demand endpoint over HTTP with a real login token, and the cosmetic/template path against live data. Consider pinning `TYPESAFE_MODEL=jev-1.14.0` so `jev-latest` cannot change behavior unnoticed (each version row records the model that answered).
8. Scheduler (APScheduler, daily cron `SCAN_CRON`) calling `ScanService.scan_source` for every active source. The manual endpoint exists; the schedule does not. Scans are sequential and slow (about 3 minutes per board), so run them one source at a time.

## Verification
- `uv run pytest` (fixtures cover: new, updated, removed after 2 misses, baseline, parse failure => needs_review, SSRF blocks).
- `uv run uvicorn irb_watch.main:app` then GET `/health`; admin-JWT call to trigger a scan against one real hospital URL.
- Check `sources.last_scan_status`, `documents`, `document_versions` and `audit_logs` rows in Supabase; confirm failures show `needs_review`.
- Create a user through the admin endpoint: a `profiles` row appears with role `user`. Subscribe and change a role: matching `audit_logs` rows appear.

## Supabase workflow (CLI v3)
- Commands target the **local** stack by default; hosted projects need `--remote`. No Docker on this machine, so there is no local stack.
- Create a migration: `npx supabase migration new <name>`, then fill the file. Never edit an applied migration; add a new one.
- Preview: `npx supabase db push --remote --dry-run`. Apply: `npx supabase db push --remote --yes`.
- Verify: `npx supabase db query --remote --read-only "<sql>"`. `information_schema` grant views are empty for the login role; use `has_table_privilege()` instead.
- The project's default privileges give `service_role` every privilege on new tables. Always `revoke all` then grant exactly what is needed.
- The CLI labels this project "production". It is the only project and has no real data yet.

## Open items
- Login-only boards (board `2605277031` on hrpp.snuh.org): login and read verified live read-only on 2026-11-06. Not yet done: a scan that writes to the DB for this source, attachment download with a session, wrong-password behaviour on the real site, and the per-user access rule (see decisions.md "Access to login-only content", needs a migration). Server-side credential storage is Windows-only for now (`keyring`); a hosted server needs an encrypted DB store behind `CredentialStore`.
- The site returns intermittent `502` (seen on `robots.txt` and the login form). Retry rules are built (see decisions.md "Temporary site errors"); not yet exercised against the real site during an actual outage.
- Which API gateway (affects Agents SDK client setup).
- Second board `2605277034` fully scanned live; HWP/DOCX samples.
- RLS from a real user's token (users see only subscribed sources); refresh-token behaviour after a ban; a real login token against the verifier. Needs the project's publishable key.
- Audit rows from live tests cannot be deleted (append-only log).
- Sign-up is turned off in the dashboard (done by the project owner).
