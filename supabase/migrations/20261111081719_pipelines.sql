-- IRB pipeline, phase 2: users' pipelines (owner, 2026-11-11; see docs/decisions.md
-- "IRB pipeline: user pipelines").
--
--   * One pipeline per project and hospital (source), started by the user, copying the hospital's
--     step order (or the global one). Owner only; the server reads and writes (no browser
--     access), so sharing can be added later in one place.
--   * Steps: copied from the order, or added by the user (from a step type or free), reordered,
--     removed softly. Status keys come from step_statuses; dates are filled by the server.
--   * Documents: named per step; each upload of the same name is a new version. Files sit in the
--     private bucket `pipeline-files`. Single versions are never deleted; a whole document,
--     step or pipeline is removed softly and purged with its files after 30 days.
--   * pipeline_events: the history (append-only for the server).
--   * A project with pipelines (even removed ones not yet purged) cannot be deleted
--     (foreign key, no action); removing a hospital from a project keeps its pipeline.
--   * organization_id on pipelines (statistics); children follow the pipeline.

create table public.pipelines (
  id              bigint generated always as identity primary key,
  user_id         uuid not null references public.profiles (id) on delete cascade,
  organization_id uuid not null default '00000000-0000-4000-8000-000000000001'
                    references public.organizations (id),
  -- NO ACTION (checked at the end of the statement), not RESTRICT: a project or source with
  -- pipelines cannot be deleted on its own, but deleting a user still cascades through both
  -- their projects and their pipelines in one statement.
  project_id      bigint not null references public.projects (id) on delete no action,
  source_id       bigint not null references public.sources (id) on delete no action,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  deleted_at      timestamptz
);

-- One live pipeline per project and hospital.
create unique index pipelines_project_source_key
  on public.pipelines (project_id, source_id) where deleted_at is null;
create index pipelines_user_idx on public.pipelines (user_id);
create index pipelines_source_idx on public.pipelines (source_id);
create index pipelines_organization_idx on public.pipelines (organization_id);
create index pipelines_deleted_idx on public.pipelines (deleted_at) where deleted_at is not null;

create table public.pipeline_steps (
  id           bigint generated always as identity primary key,
  pipeline_id  bigint not null references public.pipelines (id) on delete cascade,
  step_type_id bigint references public.step_types (id) on delete set null,
  name         text not null check (length(btrim(name)) between 1 and 100),
  position     integer not null,
  status_key   text not null references public.step_statuses (key),
  due_on       date,
  submitted_on date,
  decided_on   date,
  note         text not null default '' check (length(note) <= 4000),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  deleted_at   timestamptz
);

create index pipeline_steps_pipeline_idx on public.pipeline_steps (pipeline_id, position);
create index pipeline_steps_type_idx on public.pipeline_steps (step_type_id);
create index pipeline_steps_status_idx on public.pipeline_steps (status_key);
create index pipeline_steps_due_idx on public.pipeline_steps (due_on) where deleted_at is null;

create table public.step_documents (
  id         bigint generated always as identity primary key,
  step_id    bigint not null references public.pipeline_steps (id) on delete cascade,
  name       text not null check (length(btrim(name)) between 1 and 200),
  created_at timestamptz not null default now(),
  deleted_at timestamptz
);

create unique index step_documents_live_name_key
  on public.step_documents (step_id, lower(btrim(name))) where deleted_at is null;

create table public.step_document_versions (
  id           bigint generated always as identity primary key,
  document_id  bigint not null references public.step_documents (id) on delete cascade,
  version_no   integer not null check (version_no >= 1),
  filename     text not null check (length(filename) between 1 and 255),
  storage_path text not null unique,
  sha256       text not null,
  size_bytes   bigint not null check (size_bytes >= 0),
  -- Extracted text for the AI (PDF, HWP, HWPX, DOCX); null when the file has none.
  body_text    text,
  has_text     boolean generated always as (body_text is not null) stored,
  text_note    text,
  uploaded_by  uuid references public.profiles (id) on delete set null,
  created_at   timestamptz not null default now(),
  unique (document_id, version_no)
);

create index step_document_versions_uploaded_by_idx on public.step_document_versions (uploaded_by);

create table public.pipeline_events (
  id          bigint generated always as identity primary key,
  pipeline_id bigint not null references public.pipelines (id) on delete cascade,
  step_id     bigint references public.pipeline_steps (id) on delete set null,
  actor_id    uuid references public.profiles (id) on delete set null,
  event_type  text not null,
  payload     jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now()
);

create index pipeline_events_pipeline_idx on public.pipeline_events (pipeline_id, created_at desc);
create index pipeline_events_step_idx on public.pipeline_events (step_id);
create index pipeline_events_actor_idx on public.pipeline_events (actor_id);

alter table public.pipelines              enable row level security;
alter table public.pipeline_steps         enable row level security;
alter table public.step_documents         enable row level security;
alter table public.step_document_versions enable row level security;
alter table public.pipeline_events        enable row level security;

revoke all on table
  public.pipelines, public.pipeline_steps, public.step_documents,
  public.step_document_versions, public.pipeline_events
from public, anon, authenticated, service_role;

grant select, insert, update, delete on table
  public.pipelines, public.pipeline_steps, public.step_documents
to service_role;
-- Versions are written once; they go only with their document (purge, cascade).
grant select, insert on table public.step_document_versions to service_role;
grant select, insert on table public.pipeline_events to service_role;
-- No policy for the browser: the server serves pipelines to their owner.

-- Reorders a pipeline's live steps in one statement (all or nothing). p_step_ids must be
-- exactly the live steps, each once; otherwise nothing changes and the call fails (P0001).
create function public.reorder_pipeline_steps(p_pipeline_id bigint, p_step_ids bigint[])
returns void
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if cardinality(p_step_ids) <> (select count(distinct x) from unnest(p_step_ids) x)
     or (select coalesce(array_agg(id order by id), '{}') from public.pipeline_steps
         where pipeline_id = p_pipeline_id and deleted_at is null)
        <> (select coalesce(array_agg(x order by x), '{}') from unnest(p_step_ids) x) then
    raise exception 'the step list does not match the pipeline''s steps' using errcode = 'P0001';
  end if;
  update public.pipeline_steps s
  set position = o.n
  from unnest(p_step_ids) with ordinality as o(id, n)
  where s.id = o.id and s.pipeline_id = p_pipeline_id;
end;
$$;

revoke all on function public.reorder_pipeline_steps(bigint, bigint[]) from public, anon, authenticated;
grant execute on function public.reorder_pipeline_steps(bigint, bigint[]) to service_role;

-- Private bucket for uploaded step files (service role only, like the other buckets).
insert into storage.buckets (id, name, public, file_size_limit)
values ('pipeline-files', 'pipeline-files', false, 20000000)
on conflict (id) do nothing;

comment on table public.pipelines is
  'A user''s IRB pipeline for one hospital in one project. Owner only, served by the server.';
comment on table public.step_document_versions is
  'Versions of a step document. Never deleted one by one; purged with their document.';
