-- User projects (see docs/decisions.md, "User projects").
--
-- A project is a user's private folder of IRB sites (sources) under a name, e.g. one per study
-- they coordinate. It grants NO access: a user may add only sources they can already read
-- (private.can_read_source), and the documents of a project are still read through the normal
-- source rules. If a user later loses access to a source, the link stays but its documents and
-- name stop showing (RLS on sources/documents), and the user can remove it.
--
-- The browser writes these rows directly (no server code), like notification_prefs. Only the
-- owner can see or change a project. Limits: 50 projects per user, 30 sources per project.

create table public.projects (
  id         bigint generated always as identity primary key,
  user_id    uuid not null default auth.uid() references public.profiles (id) on delete cascade,
  name       text not null check (char_length(btrim(name)) between 1 and 100),
  note       text check (note is null or char_length(note) <= 1000),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- One name per user, ignoring case and outer spaces.
create unique index projects_user_name_key on public.projects (user_id, lower(btrim(name)));

create table public.project_sources (
  project_id bigint not null references public.projects (id) on delete cascade,
  source_id  bigint not null references public.sources (id) on delete cascade,
  added_at   timestamptz not null default now(),
  primary key (project_id, source_id)
);

create index project_sources_source_idx on public.project_sources (source_id);

alter table public.projects enable row level security;
alter table public.project_sources enable row level security;

-- The project's default privileges give service_role everything: revoke first, then grant
-- exactly what is needed. user_id is never written by the browser (it defaults to auth.uid()).
revoke all on table public.projects from public, anon, authenticated, service_role;
grant select, delete on table public.projects to authenticated;
grant insert (name, note) on table public.projects to authenticated;
grant update (name, note) on table public.projects to authenticated;
grant select, delete on table public.projects to service_role;

revoke all on table public.project_sources from public, anon, authenticated, service_role;
grant select, delete on table public.project_sources to authenticated;
grant insert (project_id, source_id) on table public.project_sources to authenticated;
grant select, delete on table public.project_sources to service_role;

create policy "projects_select_own"
  on public.projects for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy "projects_insert_own"
  on public.projects for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

create policy "projects_update_own"
  on public.projects for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

create policy "projects_delete_own"
  on public.projects for delete
  to authenticated
  using ((select auth.uid()) = user_id);

create policy "project_sources_select_own"
  on public.project_sources for select
  to authenticated
  using (exists (
    select 1 from public.projects p
    where p.id = project_id and p.user_id = (select auth.uid())
  ));

-- Adding a source needs both: the project is the user's, and the user can read the source.
create policy "project_sources_insert_own_readable"
  on public.project_sources for insert
  to authenticated
  with check (
    exists (
      select 1 from public.projects p
      where p.id = project_id and p.user_id = (select auth.uid())
    )
    and (select private.can_read_source(source_id))
  );

-- Removing works even after access to the source was lost.
create policy "project_sources_delete_own"
  on public.project_sources for delete
  to authenticated
  using (exists (
    select 1 from public.projects p
    where p.id = project_id and p.user_id = (select auth.uid())
  ));

-- updated_at follows a rename or a note change (the browser cannot write it).
create function private.touch_project()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

revoke all on function private.touch_project() from public, anon, authenticated;

create trigger touch_project
  before update on public.projects
  for each row execute function private.touch_project();

-- Size limits. Runs as the caller, so it counts the caller's own rows (RLS); two inserts in the
-- same instant could pass one over the limit, which is harmless.
create function private.check_project_limits()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_table_name = 'projects' then
    if (select count(*) from public.projects where user_id = new.user_id) >= 50 then
      raise exception 'at most 50 projects per user' using errcode = 'P0001';
    end if;
  else
    if (select count(*) from public.project_sources where project_id = new.project_id) >= 30 then
      raise exception 'at most 30 sites per project' using errcode = 'P0001';
    end if;
  end if;
  return new;
end;
$$;

-- Trigger functions need no EXECUTE grant for the caller (checked when the trigger is created).
revoke all on function private.check_project_limits() from public, anon, authenticated;

create trigger check_project_limit
  before insert on public.projects
  for each row execute function private.check_project_limits();

create trigger check_project_source_limit
  before insert on public.project_sources
  for each row execute function private.check_project_limits();

comment on table public.projects is
  'A user''s private folder of IRB sites. Grants no access; owner-only RLS.';
comment on table public.project_sources is
  'Sites in a project. Insert only for sources the user can read (private.can_read_source).';
