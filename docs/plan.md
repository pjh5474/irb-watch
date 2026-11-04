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
    migrations/             sources, documents, document_versions, changes,
                            subscriptions, scan_runs, profiles(role) + RLS
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
2. [done] `config.py`, `main.py`, `/health`, `tests/test_health.py`. Not yet run.
3. Supabase migrations + RLS; document tables in `supabase/migrations/`; private Storage bucket for originals. (Waiting on Supabase project.)
4. `auth/` JWT verify + `require_admin`; admin user-creation endpoint via Supabase Admin API.
5. [done] `security/url_guard` + `limits` + tests.
6. [partly done] SNUH adapter (`crawlers/snuh.py`, `crawlers/fetcher.py`, `core/models.py`, `core/hashing.py`) verified live on board 2605277029, 52 tests pass. **Remaining:** PDF text parser in `parsers/`, diff/removal logic in `core/` against an in-memory store, second board live check, HWP/DOCX samples. `snuh-crawler.py` in the repo root is superseded and should be deleted.
7. `ai/` summary agent behind the gateway-configurable client.
8. Scheduler + scan endpoint with run lock and caps.

## Verification
- `uv run pytest` (fixtures cover: new, updated, removed after 2 misses, baseline, parse failure => needs_review, SSRF blocks).
- `uv run uvicorn irb_watch.main:app` then GET `/health`; admin-JWT call to trigger a scan against one real hospital URL.
- Check a scan_runs row and changes rows in Supabase; confirm failures show `needs_review`.

## Open items
- Which API gateway (affects Agents SDK client setup).
- First real hospital URL and sample file, to confirm parseability (HWP especially).
