-- Users no longer subscribe to sources: every signed-in user may read every source.
--
-- Owner decision 2026-11-09: admin and the scheduler scan; users only read what was
-- scanned. A members-only source (requires_login) is still readable only with an
-- unexpired proof of access (public.source_access), which the user creates with their own
-- hospital login. Admins read everything.
--
-- The read policies of documents, document_versions, document_version_files and audit_logs
-- already call private.can_read_source(), so replacing the function is enough.
-- sources_select_authenticated already allows every signed-in user to list sources.
--
-- public.subscriptions is kept (existing rows, audit trigger) but users can no longer add
-- rows: insert is revoked so no dead data accumulates. Select and delete stay so a client
-- can still read and clear its old rows. Dropping the table is a later, separate decision.

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

revoke insert on table public.subscriptions from authenticated;
