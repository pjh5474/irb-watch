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
| Python version | Pinned to 3.12 (`>=3.12,<3.13`) | Libraries are best tested on 3.12; matches the Dockerfile. 3.14 worked but offered nothing here |
| Server layout | One FastAPI server plus Supabase. No separate BE service | A second service adds deployments, duplicate auth and failure modes. Extract later if needed |
| Schema tool | Supabase CLI SQL migrations, not SQLAlchemy/Alembic/Drizzle | Most schema work is RLS, triggers and Storage policies that Alembic cannot autogenerate |
| Profiles | `public.profiles(id uuid PK -> auth.users on delete cascade, email, display_name, role user/admin)`, created by an `auth.users` trigger | Role is never read from user-editable metadata. Users may update only `display_name` (column-level grant) |
| DB tables (MVP) | `profiles`, `sources`, `documents`, `document_versions`, `subscriptions`, `audit_logs` | Dropped `changes` and `scan_runs`: the feed comes from versions and the audit log; latest scan result lives on `sources` |
| Document model | One `documents` row per post; attachments are a JSON list inside each `document_versions` row | Avoids a files table. `change_type` is `baseline` (silent first scan) or `updated` |
| Audit log | Append-only `audit_logs`; categories document, scan, admin, user. No foreign keys, so entries outlive the rows they describe. Triggers log subscription and role changes | Feeds dashboard news cards. Users see document entries for subscribed sources; admins see all |
| Access model | Users read via supabase-js under RLS; all writes go through the server's service role; Storage objects readable only by the server, which issues signed URLs | Keeps the access checks in one place |
| JWT verification | Verify against the project's public ES256 key list; no JWT secret in `.env` | Confirmed from the project's JWKS endpoint |
| OpenAI endpoint | Empty `OPENAI_BASE_URL` means OpenAI direct; set means gateway. Gateway set without a model fails at startup; disable tracing in gateway mode | Model name is read from `.env`, never hardcoded |
| Sign-up | Public sign-up disabled (`enable_signup = false`) | Accounts are admin-created |
| Admin API scope (step 4) | Create user, list users, change role, soft delete, restore | Soft delete = `profiles.deleted_at` plus an auth ban; email stays reserved, restore undoes it |
| New user credentials | Admin sets a temporary password (min 8 chars); email auto-confirmed | No email service needed yet |
| Admin check | Role read from the database on every admin request | Demotions and deletions apply immediately; a stale token cannot keep admin rights |
| Lock-out guards | Refuse demoting or deleting yourself, and removing the last active admin | Count-then-write, so a simultaneous race is possible but unlikely at this scale |
| First admin | One-off script `irb_watch.scripts.create_first_admin`, reusable for the production project | Steps in `docs/runbook.md` |
| Role-change audit | Database trigger only, so role changes have no actor | User chose this over a server-written entry |
| Settings loading | `APP_ENV` selects `.env.<APP_ENV>`, default dev; real env vars override | Switching to production needs no code change |
| CORS | `FRONTEND_ORIGINS`, default `http://localhost:3000` | |
| Unreadable files | Two outcomes. **Hash-only** (expected, stable): scanned PDF, HWP/HWPX/DOCX with no extractor yet; tracked by file SHA-256, labelled "text not available", no alert on every scan. **Failure** (`needs_review`): corrupt, password-protected, over 500 pages, download error, or bytes that do not match a document extension (e.g. an HTML login page named .pdf) | A permanent, harmless "cannot read" must not look like a fault on every scan. Known limits: a hash changes on any byte difference, so a re-saved image PDF can look updated; hash-only files get no AI summary. OCR deferred |
| What counts as "updated" | A change in title, page text, or any attached file's bytes. A title-only change is an update with suggested priority `low`. If a file's bytes change but its extracted text does not (re-export), a silent `resaved` version is stored and no alert is raised | User decided title changes count. `resaved` stops a harmless re-save from alerting on every scan |
| Seeing what changed | Every version keeps its full page text and per-file extracted text. Changes are computed on demand between any two versions, chosen by version number or by date (`version_as_of`), as a word-level diff; `excerpt()` gives a compact view for cards and AI input | User wants users to see changes, with version and date control. Diffs over 20,000 words per side are not computed word by word |
| Change events | `new`, `updated`, `removed` (after 2 misses), `reappeared`. Failed posts go to `needs_review` and keep their old state; secret posts are neither seen nor missing; incomplete scans never count misses | Logic lives in `core/detect.py` against an in-memory store; the database layer comes later |
| Version history storage | Keep the full page text of every version and the extracted text of every file, forever. Files live in `document_version_files`; `document_versions.files` JSON was dropped | User chose completeness over storage savings so any two versions can be diffed. Revisit if the database grows large |
| Original files | Uploaded to the private bucket only when their SHA-256 is new; object name = hash; the database keeps the path | Unchanged files are never re-uploaded. Same file in many posts is stored once |
| Scan trigger | Manual first: `POST /admin/sources/{id}/scan` returns 202 and scans in the background; scheduler comes later | User chose this. The 404/409 checks happen before the 202 |
| Scan lock | `sources.scan_started_at` claimed atomically; a claim older than 30 minutes counts as a crashed scan | Two scans of one source must not overlap |
| Scan failure states | List-level layout change or incomplete list or robots block = source `needs_review`; network error or crash = `failed`. Neither changes any document. One broken post = that post `needs_review`, rest still processed | A failure must never look like "no change" or "deleted" |
| Baseline | `sources.last_scan_at` is set only by a scan that stored its results, so a failed first scan does not use up the silent baseline | Otherwise the next success would report every post as new |
| Title vs text | The title is removed from the stored page text before comparing | Otherwise a title edit would also count as a text edit |
| Adding a site | A new site needs an adapter, a reader and one branch in `api/deps.build_reader`, plus its name in the `sources.adapter` check constraint | Only `snuh_hrpp` exists today |
| Text formats | PDF, HWP 5.x, HWPX and DOCX are read; legacy `.doc` and HWP before 5.0 stay hash-only. HWP uses my own reader on `olefile` + `zlib` (BSD), not pyhwp (AGPL). DOCX keeps body, tables, headers, footers, footnotes, endnotes, text boxes and inserted tracked-change text; drops deleted text, comments and field codes | User chose all four formats and the DOCX scope. License reason for HWP. Readers parse XML with defusedxml and cap zip/deflate/text sizes; a file over a cap is refused, never cut short |
| Three outcomes for a file | **Hash-only (expected, no alert):** scanned PDF, HWP distribution document, unsupported type. **Unreadable (file-level):** real file we cannot read (corrupt, password-protected, too large, or a bug in a reader): post stays tracked, file tracked by hash, warning attached, document set to `needs_review`. **Post fails:** bytes are not the promised document (an HTML page named .pdf): post keeps its old state | Replaces the earlier rule that any parse error fails the whole post. User wants tracking to continue and the reason visible. An HTML error page must not be tracked as if it were the file |
| Failure visibility | `documents.review_reason` holds the reason; a `document.needs_review` audit entry (category `document`, so subscribers see it) is written when a post first needs review or its reason changes, not on every scan. Files keep `extract_note`. Reader bugs are logged with a traceback and shown as "internal error (ExceptionType)" | User: "users want to see why it fails". A post never stored before that fails again gets a notice each scan (no row to compare with) |
| Tests touching Supabase | Offline tests by default; live test only with `RUN_LIVE=1` | User said the project is a test account, so the live test runs against it; it leaves append-only audit rows |

## Open items
- Which API gateway. This affects how the Agents SDK client is configured (Responses API vs chat-completions mode).
- HWP/DOCX samples to confirm parseability. HWP is the main risk.
- The hosted project's own sign-up switch must be turned off by hand in the dashboard.
- Check each target site's robots.txt and terms of service.
- Success test set for detection: define known changes, and measure false positives and parse failures too.

## Process notes
- The user does not want files edited through shell commands. Use the Edit and Write tools only, so rewind works. Also saved in memory.
- `pyproject.toml` was overwritten once without being read first. Check it if anything seems missing.
- `irb_watch/notify/` and the root `snuh-crawler.py` should be deleted manually.
- Verification of the first audit-log migration found `service_role` could still update/delete rows because of the project's default privileges. Fixed by a follow-up migration. Lesson: revoke first, then grant.
- `.env.dev` was briefly not git-ignored; `.gitignore` now ignores `.env.*` except `.env.example`.
