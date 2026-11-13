-- Sharing a project (IRB pipeline redesign, phase B1; see docs/pipeline-design.md 4.8 and
-- docs/decisions.md "IRB pipeline redesign, phase B1: sharing").
--
-- Who may open a project (one rule for RLS and the server, private.project_access_level):
--   owner    projects.user_id                                          everything
--   manage   an owner/admin of the project's organization              read, edit, members, PM, hand-over
--   pm       project_members role 'pm', still in the project's org      read, edit, members
--   edit     project_members role 'member', still in the project's org  read, edit
--   read     a platform admin (profiles.role = 'admin') otherwise       read only (owner, 2026-11-13: support)
--   null     anyone else                                                nothing
-- Organizations only grant this one thing: their admins open their organization's projects.
-- A member moved to another organization loses access at once (checked on every call).
--
-- Browser: reads and edits projects and project_hospitals under these rules (lists in the
-- browser filter to the user's own and shared projects themselves); reads its own
-- project_members rows. Everything else about sharing goes through the server.

-- ---------------------------------------------------------------------------
-- project_members
-- ---------------------------------------------------------------------------
create table public.project_members (
  project_id  bigint not null references public.projects (id) on delete cascade,
  user_id     uuid   not null references public.profiles (id) on delete cascade,
  role        text   not null check (role in ('pm', 'member')),
  added_by    uuid   references public.profiles (id) on delete set null,
  added_at    timestamptz not null default now(),
  primary key (project_id, user_id)
);

create unique index project_members_one_pm on public.project_members (project_id) where role = 'pm';
create index project_members_user_idx on public.project_members (user_id);

alter table public.project_members enable row level security;

-- The project's default privileges give service_role everything: revoke first, then grant
-- exactly what is needed.
revoke all on table public.project_members from public, anon, authenticated, service_role;
grant select on table public.project_members to authenticated;
grant select, insert, update, delete on table public.project_members to service_role;

create policy "project_members_select_own"
  on public.project_members for select
  to authenticated
  using ((select auth.uid()) = user_id);

comment on table public.project_members is
  'Users a project is shared with: one PM at most, and members. Written by the server only.';

-- ---------------------------------------------------------------------------
-- The access rule
-- ---------------------------------------------------------------------------
create function private.project_access_level(p_project bigint, p_user uuid)
returns text
language sql
stable
security definer
set search_path = ''
as $$
  select case
    when p_user is null or pr.id is null then null
    when pr.user_id = p_user then 'owner'
    when exists (
      select 1 from public.organization_members om
      where om.user_id = p_user and om.organization_id = pr.organization_id
        and om.role in ('owner', 'admin')
    ) then 'manage'
    when exists (
      select 1 from public.project_members m
      join public.organization_members om
        on om.user_id = m.user_id and om.organization_id = pr.organization_id
      where m.project_id = pr.id and m.user_id = p_user and m.role = 'pm'
    ) then 'pm'
    when exists (
      select 1 from public.project_members m
      join public.organization_members om
        on om.user_id = m.user_id and om.organization_id = pr.organization_id
      where m.project_id = pr.id and m.user_id = p_user and m.role = 'member'
    ) then 'edit'
    when exists (
      select 1 from public.profiles p
      where p.id = p_user and p.role = 'admin' and p.deleted_at is null
    ) then 'read'
  end
  from (select 1) as one
  left join public.projects pr on pr.id = p_project
$$;

revoke all on function private.project_access_level(bigint, uuid) from public, anon, authenticated;

-- For the browser's policies: always the signed-in user.
create function private.can_open_project(p_project bigint)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select private.project_access_level(p_project, (select auth.uid())) is not null
$$;

create function private.can_edit_project(p_project bigint)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select private.project_access_level(p_project, (select auth.uid())) in ('owner', 'manage', 'pm', 'edit')
$$;

revoke all on function private.can_open_project(bigint) from public, anon;
revoke all on function private.can_edit_project(bigint) from public, anon;
grant execute on function private.can_open_project(bigint) to authenticated;
grant execute on function private.can_edit_project(bigint) to authenticated;

-- For the server (service_role only; the user is passed in): the same rule.
create function public.project_access(p_project bigint, p_user uuid)
returns text
language sql
stable
security definer
set search_path = ''
as $$
  select private.project_access_level(p_project, p_user)
$$;

-- The projects a user may work on (owner, manage, pm, edit): the work page and reminders.
create function public.workable_project_ids(p_user uuid)
returns setof bigint
language sql
stable
security definer
set search_path = ''
as $$
  select pr.id from public.projects pr
  where private.project_access_level(pr.id, p_user) in ('owner', 'manage', 'pm', 'edit')
$$;

revoke all on function public.project_access(bigint, uuid) from public, anon, authenticated;
revoke all on function public.workable_project_ids(uuid) from public, anon, authenticated;
grant execute on function public.project_access(bigint, uuid) to service_role;
grant execute on function public.workable_project_ids(uuid) to service_role;

-- ---------------------------------------------------------------------------
-- projects and project_hospitals: new policies
-- ---------------------------------------------------------------------------
drop policy "projects_select_own" on public.projects;
drop policy "projects_update_own" on public.projects;

-- The owner is matched on the row itself first: a new row (INSERT ... RETURNING, as the new
-- project form does) is not yet visible to the access function's own query.
create policy "projects_select_shared"
  on public.projects for select
  to authenticated
  using ((select auth.uid()) = user_id or private.can_open_project(id));

-- Column grants still limit the browser to name and note.
create policy "projects_update_shared"
  on public.projects for update
  to authenticated
  using ((select auth.uid()) = user_id or private.can_edit_project(id))
  with check ((select auth.uid()) = user_id or private.can_edit_project(id));

-- projects_insert_own and projects_delete_own stay: only the owner creates and deletes.

drop policy "project_hospitals_select_own" on public.project_hospitals;
drop policy "project_hospitals_insert_own" on public.project_hospitals;
drop policy "project_hospitals_delete_own" on public.project_hospitals;

create policy "project_hospitals_select_shared"
  on public.project_hospitals for select
  to authenticated
  using (private.can_open_project(project_id));

-- Any hospital may be added (owner, 2026-11-12); its boards stay gated per board.
create policy "project_hospitals_insert_shared"
  on public.project_hospitals for insert
  to authenticated
  with check (private.can_edit_project(project_id));

create policy "project_hospitals_delete_shared"
  on public.project_hospitals for delete
  to authenticated
  using (private.can_edit_project(project_id));

-- ---------------------------------------------------------------------------
-- projects.organization_id is now an access column: set from the creator's organization, never
-- chosen by the browser (which has no grant on it anyway).
-- ---------------------------------------------------------------------------
create function private.set_project_organization()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  org uuid;
begin
  select om.organization_id into org from public.organization_members om where om.user_id = new.user_id;
  if org is not null then
    new.organization_id = org;
  end if;
  return new;
end;
$$;

revoke all on function private.set_project_organization() from public, anon, authenticated;

create trigger set_project_organization
  before insert on public.projects
  for each row execute function private.set_project_organization();

-- ---------------------------------------------------------------------------
-- Changes that touch several rows: one transaction each (server only). The server checks who
-- may do them and writes the audit entry first.
-- ---------------------------------------------------------------------------

-- The PM: the current one becomes a member, the new one (if any) becomes the PM (added as a
-- member when needed). The unique index allows one PM; a race raises 23505.
create function public.set_project_pm(p_project bigint, p_user uuid, p_actor uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  update public.project_members set role = 'member' where project_id = p_project and role = 'pm';
  if p_user is not null then
    insert into public.project_members (project_id, user_id, role, added_by)
    values (p_project, p_user, 'pm', p_actor)
    on conflict (project_id, user_id) do update set role = 'pm';
  end if;
end;
$$;

-- Hand-over: the new owner's member row goes (an owner is never also a member), the owner
-- changes, the old owner stays as a member if asked. A name the new owner already uses raises
-- 23505 (projects_user_name_key) and nothing changes.
create function public.hand_over_project(p_project bigint, p_user uuid, p_keep boolean, p_actor uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  old_owner uuid;
begin
  select user_id into old_owner from public.projects where id = p_project for update;
  if old_owner is null then
    raise exception 'project not found' using errcode = 'P0002';
  end if;
  delete from public.project_members where project_id = p_project and user_id = p_user;
  update public.projects set user_id = p_user where id = p_project;
  if p_keep then
    insert into public.project_members (project_id, user_id, role, added_by)
    values (p_project, old_owner, 'member', p_actor)
    on conflict (project_id, user_id) do nothing;
  end if;
end;
$$;

revoke all on function public.set_project_pm(bigint, uuid, uuid) from public, anon, authenticated;
revoke all on function public.hand_over_project(bigint, uuid, boolean, uuid) from public, anon, authenticated;
grant execute on function public.set_project_pm(bigint, uuid, uuid) to service_role;
grant execute on function public.hand_over_project(bigint, uuid, boolean, uuid) to service_role;
