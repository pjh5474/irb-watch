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
- **Build every new feature SaaS-ready.** Few users now, but the goal is a multi-organisation SaaS for CRAs, and the owner does not want to restructure features for it later:
  - shape new tables and storage paths so an organisation id can be added without changing the feature, and keep data access behind the `db/` store protocols (one place per table for a tenant filter);
  - do not make per-customer settings (limits, AI instructions, "global" guides or forms) server-wide without saying so;
  - build slow work (scans, AI drafts, big files) as jobs with a status, not long requests;
  - record usage per user and action (for per-organisation limits and billing later);
  - keep hospital specifics in adapters, guides and templates, not the core;
  - name SaaS-relevant choices in the questions asked before planning.
  **Organizations only group users and give per-organization statistics; they never grant access.** A private (login or manual) source is opened to individual users by the platform admin (`source_members`); an organization that wants a login site asks the admin, who registers it with that organization's hospital login.
  **Two exceptions, built 2026-11-13 with phase B1** (`docs/pipeline-design.md` 4.8, decisions.md "IRB pipeline redesign, phase B1: sharing"; migration `project_sharing` applied 2026-11-13): an organization's owners and admins open every pipeline project of that organization; platform admins read every project (no changes). One rule decides: `private.project_access_level` (owner, manage, pm, edit, read). Private board documents stay per person.
  Not yet multi-tenant: organizations exist only as columns (migration `organizations`, see decisions.md "Organizations (SaaS step 1)"; one Default organization, nothing filters by it, defaults fill it), the scheduler and long work run inside the API process, uploads are base64 in JSON, "global" means the whole server.
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
  crawlers/          fetcher (SSRF guard, robots, delays, read-only POSTs), snuh adapter + reader,
                     rbay adapter + login pool + reader (R-BAY e-IRBs), tls (shipped intermediate
                     cert), attachments (shared file step, zip unpacking)
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
**All 31 migrations are applied** (on 2026-11-11: `pipelines`, see decisions.md "IRB pipeline: user pipelines", `pipeline_setup`, see decisions.md "IRB pipeline: admin setup", `organization_admin`, see decisions.md "Organization admin page and statistics", `one_organization_per_user`, `organizations`, see decisions.md "Organizations (SaaS step 1)", `form_templates`, see decisions.md "Docs generator: form templates", `docgen_version_inputs`, see decisions.md "Docs generator: file choice and inputs per version", `guide_admin_tools`, see "Guide library admin tools", and `manual_sources`, see "Manual sources"; on 2026-11-10: `user_projects`, private per-user folders of sites, see decisions.md "User projects"; `rbay_adapter`, see "R-BAY boards"; `file_warnings_review` and `document_reviews_table`, see "File warnings and big files") (checked with `npx supabase migration list --remote` on 2026-11-10). Each feature below has its backend and frontend built and committed. The frontend is the separate repo `irb-watch-frontend`.

Working and verified live: login and admin API, sources, manual scans of two SNUH boards (data stored in the project: sources 4 and 5), PDF/HWP/HWPX/DOCX text, change detection with version history, AI summaries (OpenAI `gpt-6-luna` + Jev), on-demand whole-document summaries, a Docker deploy to an AWS server (`docs/deploy-ec2.md`; `https://irb-api.warwarsn.online`).

Also built:
- **Scans:** login-only boards (a fresh headless login per scan, `requires_login`), atomic scan saving (`commit_scan`), 502 retry rules.
- **Newest 50 posts per board (2026-11-10):** each scan reads the whole list but opens only the newest 50 posts (`MAX_DOCS_PER_URL`, `crawlers/window.py`); older posts are neither seen nor missing and stay frozen. Tested offline only; the server's env must not still set 200. See decisions.md "Newest 50 posts per board".
- **The scheduler:** runs at 06:00 Asia/Seoul, with a startup catch-up, one worker per site, one retry after 1 hour, and a pause after a rejected login. It is off until `SCHEDULER_ENABLED=true`. One live `run_all` over sources 4, 5 and 6 succeeded; **the owner confirmed on 2026-11-13 that the 06:00 daily run works on the server**, watching about 30 sites including R-BAY boards.
- **Downloads and safety:** download links (`POST /files/{id}/download-url`), fail-closed audit entries, an encrypted credential table (`CREDENTIAL_STORE=database`).
- **Study sources (2026-11-09):** a members-only source is readable only by admins and by users an admin assigned in `source_members`. There are no subscriptions. AI is off for these sources by default (`sources.ai_enabled`). Admin routes: `/admin/sources/{id}/members` and `POST /admin/documents/{id}/summary`.
- **Email (2026-11-09):**
  - Phase 1, admin alarms and the daily report: a real test mail arrived.
  - Phase 2, the user digest with one-click unsubscribe: frontend built; the owner confirmed it works (2026-11-13).
  - Phase 3, the weekly admin usage mail (Mondays 07:00 Seoul; `POST /admin/mail/weekly-preview`): the owner confirmed it works (2026-11-13).
  - The daily admin report: the owner confirmed it works (2026-11-13). The cron weekday must be a name (`mon`); in APScheduler, numbers are off by one.
  - Mail is plain server code, never a model tool.
- **The assistant (2026-11-09):** a read-only chat per user that streams server-sent events. Admins set daily limits. Admins may get Confirm-button proposals, which the browser runs through the normal admin API. It has run against the real model. The stream through Caddy (`flush_interval -1` on the stream route) looked like streaming in the browser, but the `live_agent_check` count is still inconclusive. See decisions.md "The assistant (agent), backend".
- **Docs generator (2026-11-10):** drafts IRB submission and amendment forms from stored document versions plus per-source guides. Admins keep a guide library; a user may detach or attach guides. The draft is Markdown first, edited by hand or revised by the AI; a plain DOCX comes on demand. Saving an edit made from an older version is refused (409). An AI-off study is used only when the user ticks a box for that source, and each use is audit-logged. Backend: `irb_watch/docgen/`, `services/docgen.py`, `api/docgen.py`, `api/admin_guides.py`. The RLS test passed (rolled back). 2026-11-11: a user picks which attachments and whether the page text of each post are sent, and which earlier version its changes are listed against; a side panel on the draft page changes documents and guides for the next revision; each AI version stores its inputs (`draft_versions.inputs`, migration `docgen_version_inputs` applied 2026-11-11). Also 2026-11-11: global guides, one guide linked to many sites from the library page, rename, replace file with history (`guide_revisions`, migration `guide_admin_tools` applied 2026-11-11). Not yet checked by the owner: draft quality with the real model, Word opening the hand-made DOCX, and a draft finishing after the user leaves the page. **Form templates (2026-11-11):** a draft can fill a hospital's blank `.docx` form (one answer per field; markers, tick boxes and detected blanks; admin library with a field check, or a one-off file per draft); export fills that form (`docgen/form_fields.py`, `docgen/form_fill.py`, `api/admin_templates.py`, package `python-docx`). Migration `form_templates` applied 2026-11-11. See decisions.md "Docs generator: form templates".
- **Admin-edited AI instructions (2026-11-10):** admins add text after the fixed safety rules of the chat, the docs generator and summaries. The text has history, restore, reset and a test box, and takes effect without a restart. The safety core stays in code. Code: `ai/admin_text.py`, `services/ai_instructions.py`, `api/admin_ai.py`.

- **R-BAY boards (2026-11-10):** hospital e-IRBs on www.r-bay.co.kr, first 관련규정 / 양식함 / 공지 of 대구가톨릭대학교의료원 and 이화의대부속목동병원, one members-only source per board, login stored per hospital (site `rbay-<hospital code>`), one shared login per hospital per run; zip attachments unpacked for all sources. Built and checked live read-only on both hospitals; migration `rbay_adapter` applied; **the sources are created and scanned on the server (owner, 2026-11-13).** See decisions.md "R-BAY boards".

- **Hospitals (2026-11-12):** a platform-wide `hospitals` catalog groups boards (`sources.hospital_id`, `sources.board_kind`); projects (`project_hospitals`), pipelines, per-hospital step orders, guides (`hospital_guides`) and forms (`hospital_form_templates`) are per hospital; any hospital may be added to a project, board documents stay gated per board. Code: `services/hospitals.py`, `api/admin_hospitals.py`. A members-only manual source must name its hospital (hospital names are public). Built, offline tests only; migration `hospitals` applied 2026-11-12 (32 migrations applied now); it dropped `project_sources`, `source_guides`, `source_form_templates`, so both repos must be deployed. See decisions.md "Hospitals".

- **Draft jobs and AI drafts per pipeline step (2026-11-12, IRB pipeline phase 3):** every AI draft and revision runs as a background job (`draft_jobs`; a 2-thread pool in the API process, one API process assumed; open jobs are marked failed at the next start; 2 open per user, 1 per step, 1 revision per draft). A pipeline step's draft is written from the user's own step files (named in the dialog; starting is the consent; audit-logged) and, when the step type allows, board documents of the pipeline's hospital, with the step type's instructions, guides and form. Code: `services/draft_jobs.py`, `DocgenService.prepare_draft`, `PipelineService.context`. Built, offline tests only; migration `draft_jobs` applied 2026-11-12 (33 migrations applied now); deploy backend and frontend together (the draft routes answer 202 with a job). See decisions.md "Draft jobs" and "IRB pipeline: AI drafts per step".

- **Manual sources (2026-11-11):** adapter `manual` for hospitals with no e-IRB site; admins upload documents and new versions by hand; saved through the scan path (`commit_scan`, cards, AI summaries); never scanned. Code: `services/manual.py`, `api/admin_manual.py`. Migration `manual_sources` applied 2026-11-11. See decisions.md "Manual sources".

Next candidates:
- IRB pipeline redesign (`docs/pipeline-design.md`, 2026-11-13): phase A (one step per page, no migration) built and deployed 2026-11-13 (owner); phase A2 (`/work` and step reminders in the daily mail) built 2026-11-13, offline tests only, migration `step_reminders` applied 2026-11-13 (34 migrations applied now), committed `41b1246` / frontend `e013d91`, not deployed; phase B1 (sharing) built 2026-11-13, rolled-back RLS test passed, migration `project_sharing` applied by the owner (35 migrations applied now), committed `af1bffa` / frontend `2b6b8d4`, not yet seen in a browser with the second user; next phase B2 (sites per PI); ask each phase's open choices (section 10) before building
- search over stored document text
- check Jev's accuracy against about 20 hand-labelled real revisions
- verify RLS and the endpoints with a real user's login token (needs the project's publishable key)
- real DOCX, HWPX and protected HWP samples
- try form templates with real hospital forms (rebuild the server image first: new package `python-docx`)
