-- Study sources: admin-assigned readers, an AI switch, and one board for several accounts.
--
-- Owner decisions 2026-11-09:
--   * A study is a members-only source (requires_login) scanned with its own stored hospital
--     account. It is readable by admins and by users an admin assigned to it. The user-typed
--     "prove access" flow (source_access) is removed.
--   * AI is off by default for members-only sources: study documents may be confidential.
--   * Two studies may use the same board URL with different accounts.
--   * A non-member must not even learn that a study exists: its source row is hidden too.
--
-- Rolling this out: the old server code still reads source_access for members-only sources.
-- Only admins can read the one existing members-only source (6) until readers are assigned,
-- and no non-admin user exists yet, so nothing visible breaks in the window before the new
-- server code is deployed.

-- ---------------------------------------------------------------------------
-- source_members: who may read which members-only source
-- ---------------------------------------------------------------------------
create table public.source_members (
  source_id bigint not null references public.sources (id) on delete cascade,
  user_id   uuid   not null references public.profiles (id) on delete cascade,
  added_by  uuid   references public.profiles (id) on delete set null,
  added_at  timestamptz not null default now(),
  primary key (source_id, user_id)
);

create index source_members_user_idx on public.source_members (user_id);

alter table public.source_members enable row level security;

-- The project's default privileges give service_role everything: revoke first, then grant
-- exactly what is needed. Users may only read their own rows; only the server writes.
revoke all on table public.source_members from public, anon, authenticated, service_role;
grant select on table public.source_members to authenticated;
grant select, insert, delete on table public.source_members to service_role;

create policy "source_members_select_own"
  on public.source_members for select
  to authenticated
  using ((select auth.uid()) = user_id);

comment on table public.source_members is
  'Users an admin assigned to a members-only source (a study). Admins read every source.';

-- ---------------------------------------------------------------------------
-- The read rule: admin, or a public source, or a member of the members-only source
-- ---------------------------------------------------------------------------
create or replace function private.can_read_source(p_source_id bigint)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select
    (select private.is_admin())
    or (
      -- the source must exist; an unknown id is never readable
      exists (select 1 from public.sources src where src.id = p_source_id)
      and (
        not exists (
          select 1 from public.sources src
          where src.id = p_source_id and src.requires_login
        )
        or exists (
          select 1 from public.source_members m
          where m.user_id = (select auth.uid())
            and m.source_id = p_source_id
        )
      )
    );
$$;

revoke all on function private.can_read_source(bigint) from public, anon;
grant execute on function private.can_read_source(bigint) to authenticated;

-- Source rows follow the same rule, so a non-member does not see a study's name.
drop policy "sources_select_authenticated" on public.sources;
create policy "sources_select_readable"
  on public.sources for select
  to authenticated
  using ((select private.can_read_source(id)));

-- ---------------------------------------------------------------------------
-- The old proof of access is gone (it held no rows)
-- ---------------------------------------------------------------------------
drop table public.source_access;

-- ---------------------------------------------------------------------------
-- AI switch
-- ---------------------------------------------------------------------------
alter table public.sources add column ai_enabled boolean not null default true;
update public.sources set ai_enabled = false where requires_login;

comment on column public.sources.ai_enabled is
  'False: no text of this source goes to the AI services; scan cards get a plain template line and summaries are refused.';

-- ---------------------------------------------------------------------------
-- One board, several accounts: unique per (url, login label), not per url
-- ---------------------------------------------------------------------------
alter table public.sources drop constraint sources_url_key;
create unique index sources_url_login_key
  on public.sources (url, (coalesce(config ->> 'login_user', '')));
