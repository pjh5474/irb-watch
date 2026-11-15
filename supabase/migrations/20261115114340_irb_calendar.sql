-- IRB calendar, CAL-1 (owner, 2026-11-15; see docs/decisions.md "IRB calendar, CAL-1" and
-- docs/pipeline-design.md 14.5).
--
--   * session_kinds: an admin list of review kinds (정기심의, 신속심의, 기타), platform-wide.
--   * irb_sessions: a hospital's IRB meetings with their submission cutoff. Platform-wide ON
--     PURPOSE (like hospitals): every organization reads the same rows. Entered by platform
--     admins, proposed by users or by an admin-started AI job (state 'proposed'); an admin
--     approves (tentative / confirmed) or rejects. A time may be unknown (null), never made up.
--   * session_jobs: the AI "find sessions" job per hospital (status + reason), like draft_jobs.
--   * pipeline_steps.target_session_id: the meeting a step aims for.
--   * project_tasks: dated items of a project that are not steps; organization follows the
--     project.
-- Every table is server-only (RLS on, no browser grants), like the pipeline tables.

-- 1. Review kinds --------------------------------------------------------------------------------

create table public.session_kinds (
  key         text primary key check (key ~ '^[a-z][a-z0-9_]{0,39}$'),
  label       text not null check (length(btrim(label)) between 1 and 60),
  position    integer not null,
  archived_at timestamptz,
  created_at  timestamptz not null default now()
);

insert into public.session_kinds (key, label, position) values
  ('regular',   '정기심의', 1),
  ('expedited', '신속심의', 2),
  ('other',     '기타',     3);

alter table public.session_kinds enable row level security;
revoke all on table public.session_kinds from public, anon, authenticated, service_role;
grant select, insert, update on table public.session_kinds to service_role;
comment on table public.session_kinds is
  'Kinds of IRB review sessions (admin-edited, platform-wide; archived, never deleted).';

-- 2. The AI job ---------------------------------------------------------------------------------

create table public.session_jobs (
  id             uuid primary key default gen_random_uuid(),
  hospital_id    bigint not null references public.hospitals (id) on delete cascade,
  user_id        uuid not null references public.profiles (id) on delete cascade,
  status         text not null default 'queued'
                   check (status in ('queued', 'running', 'done', 'failed')),
  error          text check (error is null or length(error) <= 1000),
  -- Which server runs the job (APP_ENV), as draft_jobs: at its start a server fails only its own.
  runner         text not null default 'dev' check (length(runner) between 1 and 50),
  documents_read integer not null default 0,
  proposed       integer not null default 0,
  skipped        integer not null default 0,
  created_at     timestamptz not null default now(),
  started_at     timestamptz,
  finished_at    timestamptz
);

create index session_jobs_hospital_idx on public.session_jobs (hospital_id, created_at desc);
create index session_jobs_user_idx on public.session_jobs (user_id, created_at desc);
-- One open job per hospital.
create unique index session_jobs_open_hospital_key
  on public.session_jobs (hospital_id) where status in ('queued', 'running');

alter table public.session_jobs enable row level security;
revoke all on table public.session_jobs from public, anon, authenticated, service_role;
grant select, insert, update, delete on table public.session_jobs to service_role;  -- delete: purge
comment on table public.session_jobs is
  'AI jobs that read a hospital''s stored posts and write session proposals. Server only.';

-- 3. Sessions -----------------------------------------------------------------------------------

create table public.irb_sessions (
  id                  bigint generated always as identity primary key,
  -- A hospital with sessions is not deleted (hospitals are removed only when empty).
  hospital_id         bigint not null references public.hospitals (id) on delete no action,
  kind_key            text not null references public.session_kinds (key),
  meets_on            date not null,
  meets_at            time,                     -- null: time unknown
  cutoff_on           date,                     -- the submission cutoff, when known
  cutoff_at           time,
  timezone            text not null default 'Asia/Seoul' check (length(timezone) between 1 and 60),
  state               text not null default 'proposed'
                        check (state in ('proposed', 'tentative', 'confirmed', 'cancelled', 'rejected')),
  origin              text not null check (origin in ('admin', 'user', 'ai')),
  note                text not null default '' check (length(note) <= 2000),
  evidence_version_id bigint references public.document_versions (id) on delete set null,
  evidence_url        text check (evidence_url is null or (length(evidence_url) <= 1000 and evidence_url ~* '^https?://')),
  evidence_quote      text check (evidence_quote is null or length(evidence_quote) <= 300),
  proposed_by         uuid references public.profiles (id) on delete set null,
  proposed_at         timestamptz,
  reviewed_by         uuid references public.profiles (id) on delete set null,
  reviewed_at         timestamptz,
  job_id              uuid references public.session_jobs (id) on delete set null,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  deleted_at          timestamptz,
  check (cutoff_on is null or cutoff_on <= meets_on),
  check (cutoff_at is null or cutoff_on is not null)
);

-- One live, non-rejected session per hospital, kind and meeting day.
create unique index irb_sessions_day_key
  on public.irb_sessions (hospital_id, kind_key, meets_on)
  where deleted_at is null and state <> 'rejected';
create index irb_sessions_hospital_idx on public.irb_sessions (hospital_id, meets_on);
create index irb_sessions_proposed_idx on public.irb_sessions (created_at) where state = 'proposed' and deleted_at is null;
create index irb_sessions_job_idx on public.irb_sessions (job_id) where job_id is not null;
create index irb_sessions_version_idx on public.irb_sessions (evidence_version_id) where evidence_version_id is not null;

alter table public.irb_sessions enable row level security;
revoke all on table public.irb_sessions from public, anon, authenticated, service_role;
grant select, insert, update on table public.irb_sessions to service_role;  -- removed = deleted_at
comment on table public.irb_sessions is
  'IRB meetings of a hospital with their submission cutoff (platform-wide on purpose). Server only.';

create trigger touch_irb_session
  before update on public.irb_sessions
  for each row execute function private.touch_project();

-- 4. The meeting a step aims for ----------------------------------------------------------------

alter table public.pipeline_steps
  add column target_session_id bigint references public.irb_sessions (id) on delete set null;
create index pipeline_steps_target_session_idx on public.pipeline_steps (target_session_id)
  where target_session_id is not null;
comment on column public.pipeline_steps.target_session_id is
  'The IRB meeting this step aims for (irb_sessions; the calendar shows its cutoff).';

-- 5. Project tasks ------------------------------------------------------------------------------

create table public.project_tasks (
  id              bigint generated always as identity primary key,
  project_id      bigint not null references public.projects (id) on delete cascade,
  organization_id uuid not null default '00000000-0000-4000-8000-000000000001'
                    references public.organizations (id),
  title           text not null check (length(btrim(title)) between 1 and 200),
  due_on          date not null,
  due_at          time,
  note            text not null default '' check (length(note) <= 4000),
  site_id         bigint references public.project_sites (id) on delete set null,
  step_id         bigint references public.pipeline_steps (id) on delete set null,
  assignee_id     uuid references public.profiles (id) on delete set null,
  done_at         timestamptz,
  done_by         uuid references public.profiles (id) on delete set null,
  created_by      uuid references public.profiles (id) on delete set null,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  deleted_at      timestamptz
);

create index project_tasks_project_idx on public.project_tasks (project_id, due_on);
create index project_tasks_due_idx on public.project_tasks (due_on) where deleted_at is null and done_at is null;
create index project_tasks_organization_idx on public.project_tasks (organization_id);
create index project_tasks_site_idx on public.project_tasks (site_id) where site_id is not null;
create index project_tasks_step_idx on public.project_tasks (step_id) where step_id is not null;
create index project_tasks_assignee_idx on public.project_tasks (assignee_id) where assignee_id is not null;

alter table public.project_tasks enable row level security;
revoke all on table public.project_tasks from public, anon, authenticated, service_role;
grant select, insert, update, delete on table public.project_tasks to service_role;  -- delete: purge
comment on table public.project_tasks is
  'Dated items of a project that are not steps (internal review day, sponsor meeting). Server only.';

create trigger touch_project_task
  before update on public.project_tasks
  for each row execute function private.touch_project();

-- A task's organization is its project's; its site and step belong to the same project (the
-- service checks first; this catches a caller that does not).
create function private.check_project_task()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  select p.organization_id into new.organization_id from public.projects p where p.id = new.project_id;
  if new.site_id is not null and not exists (
    select 1 from public.project_sites s where s.id = new.site_id and s.project_id = new.project_id
  ) then
    raise exception 'the site is not in this project' using errcode = 'P0001';
  end if;
  if new.step_id is not null and not exists (
    select 1 from public.pipeline_steps st join public.pipelines pl on pl.id = st.pipeline_id
    where st.id = new.step_id and pl.project_id = new.project_id
  ) then
    raise exception 'the step is not in this project' using errcode = 'P0001';
  end if;
  return new;
end;
$$;

revoke all on function private.check_project_task() from public, anon, authenticated;

create trigger check_project_task
  before insert or update of project_id, site_id, step_id on public.project_tasks
  for each row execute function private.check_project_task();
