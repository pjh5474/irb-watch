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
                            document_versions, subscriptions, audit_logs,
                            private `documents` bucket, RLS (3 applied so far)
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
`notify/` is out of the MVP and should be deleted.

## Implementation steps
1. [done] `pyproject.toml` for uv, `.env.example`, `Dockerfile`.
2. [done] `config.py`, `main.py`, `/health`, `tests/test_health.py`. Python pinned to 3.12.
3. [done] Supabase migrations applied to project `jlkkoaugtovvaxrqxoyx` and verified with read-only queries: `profiles` + signup trigger, domain tables + audit log + private bucket, audit-log append-only fix. **Not yet exercised with real users:** RLS policies and the three triggers.
4. [done] JWT verification (`auth/jwt.py`, ES256/RS256 via the public key list, no secret), `require_admin` (role looked up in the DB on every call), admin API `/admin/users` (create, list, change role, soft delete, restore) with lock-out guards, `profiles.deleted_at` migration, first-admin script, `APP_ENV` settings loader, CORS. 108 offline tests plus a live lifecycle test (`RUN_LIVE=1`) that passed against the real project. Commands in `docs/runbook.md`. **Not yet verified:** RLS from a real user's token, refresh-token behaviour after a ban, a real login token against the verifier. **No admin exists yet:** run the first-admin script.
5. [done] `security/url_guard` + `limits` + tests.
6. [partly done] SNUH adapter (`crawlers/snuh.py`, `crawlers/fetcher.py`, `core/models.py`, `core/hashing.py`) verified live on board 2605277029, 52 tests pass. PDF text parser done (`parsers/pdf.py`, `parsers/formats.py`; `extract_text()` returns text or a hash-only marker, real failures raise `ParseError`); verified on real SNUH files. Diff and change-detection logic done and tested against an in-memory store (`core/state.py`, `core/diff.py`, `core/detect.py`): baseline, new, updated, resaved, removed after 2 misses, reappeared, needs_review, version lookup by number or date. **Remaining:** a migration for what the logic needs (a per-version files table holding each file's hash and extracted text, `change_type` values `new` and `resaved`, audit event types `document.reappeared`), the glue that turns a `BoardSnapshot` into `Observation`s (download, `extract_text`, upload originals), the Supabase-backed store, second board live check, HWP/DOCX extractors (currently hash-only). `snuh-crawler.py` in the repo root is superseded and should be deleted.
7. `ai/` summary agent behind the gateway-configurable client.
8. Scheduler + scan endpoint with run lock and caps.

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
- Which API gateway (affects Agents SDK client setup).
- Second board `2605277034` fully scanned live; HWP/DOCX samples.
- Dashboard setting to check by hand: Authentication → disable "Allow new users to sign up" on the hosted project (`enable_signup = false` is set only in local `config.toml`).
