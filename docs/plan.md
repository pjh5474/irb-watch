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
8. Scheduler: built 2026-11-06 (`services/runner.py`, `scheduler.py`, `POST /admin/scans/run-all`); see decisions.md "Scheduler". The `scans_paused_reason` migration is applied; set `SCHEDULER_ENABLED=true` on the one server that should scan. Not yet run for real: the daily trigger itself, the startup catch-up and the 1-hour retry have only been tested with fakes and a short in-process scheduler start.

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
State on 2026-11-10: all 20 migrations up to `20261110045713_rbay_adapter.sql` are applied, and the features below are built in the backend and in the frontend repo and committed. What each item still lacks:
- **R-BAY boards (2026-11-10):** `crawlers/rbay.py`, `rbay_login.py`, `rbay_reader.py`, `tls.py` + `certs/`, zip support in `parsers/archive.py` and `crawlers/attachments.py` (used by SNUH too), `scripts/live_rbay_check.py`. Offline tests pass; read-only live check passed on 양식함 of two hospitals: 대구가톨릭대학교의료원 (6/6 posts, a .hwp with text) and 이화의대부속목동병원 (29/29 posts, a real zip unpacked into 4 files with text); logout confirmed both times. Migration `20261110045713_rbay_adapter.sql` applied 2026-11-10. Next: deploy, store the login under site `rbay` on the server, create the 6 sources (REGLA, FORM_BOX, NOTI for each hospital) and turn AI on, assign members, one manual scan each (writes data: owner's go). A daily run makes 2 R-BAY logins (one per hospital). After the code-review fixes (error classification, rejection memory per hospital, person-started retry, one fresh login for an expired hand-over, logout race, parse-before-store, zip member cap), live checks passed again: 대구가톨릭 공지 (121/121 posts listed, one post opened) and 이화목동 관련규정 (21/21 listed, a 513 KB PDF downloaded with text). The browser-error paths themselves (timeouts, refused entry) are not tested: they need a misbehaving site. Not verified: Chromium on EC2 (Linux) with R-BAY's broken certificate chain, 공지 of 이화목동 live, a full 121-post 공지 scan's duration, R-BAY's terms of use.
- **User projects (2026-11-10):** migration `20261110035051_user_projects.sql` applied 2026-11-10; rolled-back RLS test passed (own projects only, other user's project invisible and unchangeable; members-only source refused for a non-member, allowed for a member; a lost site's link stays visible without its name and can be removed; `user_id` cannot be written; duplicate name 23505; limits 50/30 raise P0001; anon denied; no rows left). Frontend `/projects` and `/projects/[id]` built (tested with a fake Supabase client only); `database.d.ts` types added by hand, to be replaced by `npm run gen:db-types` after the migration.
- **Admin-edited AI instructions (2026-11-10):** `ai/admin_text.py`, `services/ai_instructions.py`, `db/ai_instructions.py`, `api/admin_ai.py`; admin page with one editor per feature, history with restore, reset and a test box. Checked: `authenticated` has no access to `ai_instructions`. Unknown: how closely the real model follows the added text.
- **Docs generator (2026-11-10):**
  - Code: `irb_watch/docgen/`, `services/docgen.py`, `db/docgen.py`, `api/docgen.py`, `api/admin_guides.py`. The purge runs in the 03:30 job.
  - Frontend: Drafts page, new-draft form, editor with revise/compare/export, admin guide library and a guides box on each source.
  - The rolled-back RLS test passed: a user reads only their own drafts and versions, cannot write them, and cannot read `guides` or `ai_instructions`.
  - Owner to check: draft quality with the real model and real hospital guides, how long a real call takes (the request waits; there is no streaming), whether Word opens the hand-made DOCX, and the frontend screens in a browser.
  - Later: an admin-set layout per document type.
- **The assistant (2026-11-09):**
  - Code: `irb_watch/agent/`, `services/agent.py`, `db/agent.py`, `api/agent.py`, `api/admin_agent.py`, and the `chat-purge` job. It has run against the real model.
  - Caddy sends the stream route with `flush_interval -1` and no compression. In the browser the answer looked streamed, but `live_agent_check` check 5 still counts too few separate moments, so streaming through Caddy is inconclusive.
  - Not yet seen: how the model reacts to injected document text.
- **Email (2026-11-09):**
  - Phase 1 (alarms and the daily admin report): a real test mail arrived.
  - Phase 2 (user digest, unsubscribe page): frontend built.
  - Phase 3 (weekly admin stats, `POST /admin/mail/weekly-preview`): a real Monday 07:00 KST send and how the table looks in mail apps are not confirmed.
- **Study sources and the no-subscription access rule (2026-11-09):** migrations applied, frontend admin screens built. Not yet tested: a real study account login and a real non-admin user's token.
- **Next candidates:** search over stored document text; a check of Jev's accuracy on about 20 hand-labelled revisions; RLS checked with a real user's token; `npm run gen:api-types` in the frontend to replace the hand-written `docgen.ts` and `ai.ts` types.
- File download links are built and checked live for the signer (real stored file: 200, hash matches, filename header set; the same object without a link and an expired link both answer 400). The endpoint itself has only run with fakes; it has not been called with a real user's login token.
- Login-only boards (board `2605277031` on hrpp.snuh.org): login and read verified live read-only on 2026-11-06. Not yet done: a scan that writes to the DB for this source, attachment download with a session, wrong-password behaviour on the real site, and the per-user access rule (see decisions.md "Access to login-only content", needs a migration). Server-side credential storage is Windows-only for now (`keyring`); a hosted server needs an encrypted DB store behind `CredentialStore`.
- The site returns intermittent `502` (seen on `robots.txt` and the login form). Retry rules are built (see decisions.md "Temporary site errors"); not yet exercised against the real site during an actual outage.
- Which API gateway (affects Agents SDK client setup).
- Second board `2605277034` fully scanned live; HWP/DOCX samples.
- RLS from a real user's token (users see only subscribed sources); refresh-token behaviour after a ban; a real login token against the verifier. Needs the project's publishable key.
- Audit rows from live tests cannot be deleted (append-only log).
- Sign-up is turned off in the dashboard (done by the project owner).
