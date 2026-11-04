# IRB Watch: design interview record

Recorded 2026-11-04. Each entry is a question asked before planning, the answer given, and the reason. The approved plan is in `docs/plan.md`.

## Scope
- This repo is the **server only**. The frontend (Next.js) lives in a separate repo.
- Supabase (Postgres + Auth) is the database. The schema and migrations live **in this repo** under `supabase/migrations/`.
- The frontend reads data **directly from Supabase** (supabase-js + RLS). This server mostly writes, using the service-role key, and exposes few endpoints.

## Decisions

| Topic | Decision | Reason / note |
|---|---|---|
| Language | Python 3.11+, managed with uv | Better parsing libraries (PDF, HWP, OCR) than JS |
| Framework | FastAPI | Async, typed, auto OpenAPI docs |
| Agent role | Fixed pipeline. The OpenAI agent only summarizes diffs and ranks priority | Change detection must be deterministic and reproducible |
| Model control | `OPENAI_BASE_URL` + `OPENAI_MODEL` env vars, pointing at an OpenAI-compatible API gateway | User wants to switch models at the gateway. **Gateway not chosen yet** |
| URL source | Admin-managed global list. Users subscribe to items from it | Each URL is scanned once however many users follow it |
| Accounts | Created by admin only. Users log in with Supabase Auth afterwards | |
| Server auth | Verify Supabase JWT. Admin role gates admin endpoints (manage URLs, create users, trigger scans) | |
| Trigger | Built-in scheduler (APScheduler) plus an API endpoint | |
| Deploy | Single long-running container. A DB lock keeps later scaling safe | |
| Trackable unit | Linked files (PDF/HWP/DOCX) plus the page's own text | Catches notices as well as attachments |
| Storage | **Revised 2026-11-04:** store originals in a **private Supabase Storage bucket**, named by sha256. DB keeps the storage path (not a URL), hash, extracted text and the source post URL. Frontend gets short-lived signed URLs | Hospitals delete old versions; the site's download links are encrypted tokens, not permanent. Cloudflare R2 considered and deferred (negligible gain at this volume). Check hospital terms before sharing copies beyond internal users |
| Fetching | httpx by default. Playwright fallback per URL, set by a `needs_browser` flag | Keeps the image light unless JS rendering is needed |
| First scan | Silent baseline, no "new" alerts | Avoids flooding users |
| Removals | Mark removed only after **2 consecutive misses** | A flaky page load must not look like mass deletion |
| Failures | Mark `needs_review` and keep the previous state. Never treat as "no change" or "deleted" | A failure that looks like "no updates" is the worst outcome |
| URL safety | Block private/internal IPs, http/https only, respect robots.txt | Admin-only URLs, but still guard against SSRF |
| Limits | Max file size, max docs per URL, per-domain delay, cap on LLM calls per run | Bounds OpenAI cost and load on hospital sites |
| Summary language | Same language as the source document | Avoids mistranslating regulatory wording |
| Notifications | Dashboard only for the MVP. No email | Email can be added later |

## Open items
- Which API gateway. This affects how the Agents SDK client is configured (Responses API vs chat-completions mode).
- First real hospital URL plus one sample file, to confirm parseability. HWP is the main risk.
- Check each target site's robots.txt and terms of service.
- Success test set for detection: define known changes, and measure false positives and parse failures too.

## Process notes
- The user does not want files edited through shell commands. Use the Edit and Write tools only, so rewind works. Also saved in memory.
- `pyproject.toml` was overwritten once without being read first. Check it if anything seems missing.
- `irb_watch/notify/` is out of the MVP and should be deleted manually.
