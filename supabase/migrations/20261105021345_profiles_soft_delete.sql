-- Soft delete for users. The server sets deleted_at and also bans the auth account,
-- so the person cannot sign in; the profile and everything linked to it is kept.
alter table public.profiles add column deleted_at timestamptz;

comment on column public.profiles.deleted_at is
  'Set by the server (service role) when an admin removes a user. Null means active.';

-- A soft-deleted admin must not count as an admin in any RLS policy.
create or replace function private.is_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.profiles
    where id = (select auth.uid())
      and role = 'admin'
      and deleted_at is null
  );
$$;
