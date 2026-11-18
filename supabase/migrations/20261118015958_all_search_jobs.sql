-- ALL-SEARCH, slice 3 of the IRB date recommendations (owner, 2026-11-18; docs/decisions.md
-- "IRB date recommendations for users").
--
-- An admin reads a hospital's whole backlog (all its own boards, up to 500 posts per board, in
-- batches) and then lets the AI look for IRB dates in the stored posts. Each of the two steps is a
-- job with a status, a readable reason when it fails, progress per board and a stop flag.
-- Separate from session_jobs on purpose: that table allows one open job per hospital for the
-- ordinary admin search, and an hour-long read must not block it.
-- Server only, like the other session tables. Platform-wide, like hospitals and sessions.

create table public.session_search_jobs (
  id             uuid primary key default gen_random_uuid(),
  hospital_id    bigint not null references public.hospitals (id) on delete cascade,
  user_id        uuid not null references public.profiles (id) on delete cascade,
  stage          text not null check (stage in ('read', 'ai')),
  status         text not null default 'queued'
                   check (status in ('queued', 'running', 'done', 'stopped', 'failed')),
  stop_requested boolean not null default false,
  error          text check (error is null or length(error) <= 1000),
  -- Which server runs the job (APP_ENV), as the other job tables: a start-up fails only its own.
  runner         text not null default 'dev' check (length(runner) between 1 and 50),
  -- read: {"boards": {"<source id>": {"name", "stored", "total", "left", "failed", "flagged",
  --         "state", "error"}}}; ai: {"batches", "batches_done", "posts", "proposed", "skipped",
  --         "read_versions": [version ids]}
  progress       jsonb not null default '{}'::jsonb,
  created_at     timestamptz not null default now(),
  started_at     timestamptz,
  finished_at    timestamptz
);

create index session_search_jobs_hospital_idx on public.session_search_jobs (hospital_id, created_at desc);
-- One open ALL-SEARCH job per hospital (a read and an AI step never overlap).
create unique index session_search_jobs_open_hospital_key
  on public.session_search_jobs (hospital_id) where status in ('queued', 'running');

alter table public.session_search_jobs enable row level security;
revoke all on table public.session_search_jobs from public, anon, authenticated, service_role;
grant select, insert, update, delete on table public.session_search_jobs to service_role;  -- delete: purge
comment on table public.session_search_jobs is
  'ALL-SEARCH jobs per hospital: reading the backlog and the AI search over it. Server only.';
