-- Access rule for login-only boards.
--
-- Content scanned from a members-only board may be read only by users who proved, with
-- their own login on the hospital site, that they may see it, AND who follow the source.
-- Before this migration any signed-in user could insert a subscription and read it.
--
--   sources.requires_login   marks such sources. It is set when an admin gives a source a
--                            login_user and is never cleared automatically: removing the
--                            scanning login must not make private data public.
--   source_access            one row per (user, source) = "this user proved access until
--                            expires_at". Written only by the server after a successful
--                            login with the user's own credentials (never stored).
--   private.can_read_source  the single rule used by every read policy below.

alter table public.sources add column requires_login boolean not null default false;

update public.sources set requires_login = true where config ? 'login_user';

comment on column public.sources.requires_login is
  'True for members-only boards: readable only by admins and by subscribers with a valid source_access row.';

-- ---------------------------------------------------------------------------
-- source_access
-- ---------------------------------------------------------------------------
create table public.source_access (
  user_id      uuid   not null references public.profiles (id) on delete cascade,
  source_id    bigint not null references public.sources (id) on delete cascade,
  verified_at  timestamptz not null default now(),
  expires_at   timestamptz not null,
  primary key (user_id, source_id)
);

create index source_access_source_idx on public.source_access (source_id);

alter table public.source_access enable row level security;

revoke all on table public.source_access from anon, authenticated, service_role;
-- Users may see their own proofs (to show "access until ..."); only the server writes them.
grant select on table public.source_access to authenticated;
grant select, insert, update, delete on table public.source_access to service_role;

create policy "source_access_select_own"
  on public.source_access for select
  to authenticated
  using ((select auth.uid()) = user_id);

-- ---------------------------------------------------------------------------
-- The rule
-- ---------------------------------------------------------------------------
create function private.can_read_source(p_source_id bigint)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select
    (select private.is_admin())
    or (
      exists (
        select 1 from public.subscriptions s
        where s.user_id = (select auth.uid()) and s.source_id = p_source_id
      )
      and (
        not exists (
          select 1 from public.sources src
          where src.id = p_source_id and src.requires_login
        )
        or exists (
          select 1 from public.source_access a
          where a.user_id = (select auth.uid())
            and a.source_id = p_source_id
            and a.expires_at > now()
        )
      )
    );
$$;

revoke all on function private.can_read_source(bigint) from public, anon;
grant execute on function private.can_read_source(bigint) to authenticated;

-- ---------------------------------------------------------------------------
-- Read policies: same names as before, now using the rule
-- ---------------------------------------------------------------------------
drop policy "documents_select_subscribed_or_admin" on public.documents;
create policy "documents_select_subscribed_or_admin"
  on public.documents for select
  to authenticated
  using ((select private.can_read_source(source_id)));

drop policy "document_versions_select_subscribed_or_admin" on public.document_versions;
create policy "document_versions_select_subscribed_or_admin"
  on public.document_versions for select
  to authenticated
  using ((select private.can_read_source(source_id)));

drop policy "document_version_files_select_subscribed_or_admin" on public.document_version_files;
create policy "document_version_files_select_subscribed_or_admin"
  on public.document_version_files for select
  to authenticated
  using ((select private.can_read_source(source_id)));

-- News cards: document entries of readable sources; admins see every category.
drop policy "audit_logs_select_documents_subscribed_or_admin" on public.audit_logs;
create policy "audit_logs_select_documents_subscribed_or_admin"
  on public.audit_logs for select
  to authenticated
  using (
    (select private.is_admin())
    or (category = 'document' and (select private.can_read_source(source_id)))
  );
