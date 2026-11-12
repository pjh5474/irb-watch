-- Draft jobs and pipeline step drafts (owner, 2026-11-12; see docs/decisions.md
-- "IRB pipeline: AI drafts per step" and "Draft jobs").
--
--   * Every AI draft and AI revision runs as a background job with a status (queued, running,
--     done, failed + reason). The request is checked and counted before the job is queued; the
--     job itself runs inside the API process for now, so the inputs are held in memory and a
--     job that was open when the server stopped is marked failed at the next start (never run
--     again by itself).
--   * Limits (also checked by the server): one open job per pipeline step, one open revision
--     per draft. The two unique indexes below make them race-proof.
--   * A draft may belong to a pipeline step (drafts.pipeline_step_id) and may be written from
--     the user's own step files (drafts.step_files: which document versions). Such drafts are
--     not purged after 30 days; they go with their pipeline.
--   * The server writes and reads the jobs (service role). The browser has no access.

create table public.draft_jobs (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references public.profiles (id) on delete cascade,
  organization_id  uuid not null default '00000000-0000-4000-8000-000000000001'
                     references public.organizations (id),
  kind             text not null check (kind in ('draft', 'revision')),
  status           text not null default 'queued'
                     check (status in ('queued', 'running', 'done', 'failed')),
  error            text check (error is null or length(error) <= 1000),
  -- A revision names its draft from the start; a new draft gets it when the job is done.
  draft_id         uuid references public.drafts (id) on delete cascade,
  pipeline_step_id bigint references public.pipeline_steps (id) on delete set null,
  -- Which server runs the job (APP_ENV: "prod", "dev"). At its start a server fails only its
  -- own open jobs, so a developer's server on the same project never fails the live one's.
  runner           text not null default 'dev' check (length(runner) between 1 and 50),
  created_at       timestamptz not null default now(),
  started_at       timestamptz,
  finished_at      timestamptz
);

create index draft_jobs_user_idx on public.draft_jobs (user_id, created_at desc);
create index draft_jobs_organization_idx on public.draft_jobs (organization_id);
create index draft_jobs_draft_idx on public.draft_jobs (draft_id);
create index draft_jobs_step_idx on public.draft_jobs (pipeline_step_id);
create index draft_jobs_open_idx on public.draft_jobs (status) where status in ('queued', 'running');

-- One open job per pipeline step, one open revision per draft.
create unique index draft_jobs_open_step_key
  on public.draft_jobs (pipeline_step_id)
  where pipeline_step_id is not null and status in ('queued', 'running');
create unique index draft_jobs_open_revision_key
  on public.draft_jobs (draft_id)
  where kind = 'revision' and status in ('queued', 'running');

alter table public.draft_jobs enable row level security;

revoke all on table public.draft_jobs from public, anon, authenticated, service_role;
grant select, insert, update, delete on table public.draft_jobs to service_role;
-- No policy for the browser: the server serves a user's own jobs.

-- ---------------------------------------------------------------------------
-- drafts made in a pipeline step
-- ---------------------------------------------------------------------------
alter table public.drafts
  -- ON DELETE CASCADE: a step is deleted only by the 30-day purge of removed pipelines and
  -- steps (or with its user); its drafts go with it.
  add column pipeline_step_id bigint references public.pipeline_steps (id) on delete cascade,
  -- [{"document_id": 1, "version_no": 2, "name": "Protocol", "step_id": 5, "step_name": "..."}]
  add column step_files jsonb not null default '[]'::jsonb;

create index drafts_step_idx on public.drafts (pipeline_step_id) where pipeline_step_id is not null;

comment on table public.draft_jobs is
  'AI draft and revision jobs (queued, running, done, failed). Server only; run in the API process.';
comment on column public.drafts.pipeline_step_id is
  'The pipeline step this draft was made in (null: a standalone draft). Not purged after 30 days.';
comment on column public.drafts.step_files is
  'The user''s own step files (document versions) the draft is written from.';
