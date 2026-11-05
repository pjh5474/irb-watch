-- profiles: one row per auth user. Accounts are created by an admin only
-- (public sign-up is disabled), and the trigger below mirrors each new user here.

create schema if not exists private;

create table public.profiles (
  id           uuid primary key references auth.users (id) on delete cascade,
  email        text,
  display_name text,
  role         text not null default 'user' check (role in ('user', 'admin')),
  created_at   timestamptz not null default now()
);

comment on column public.profiles.role is
  'Set only by the server (service role). Never derived from user-editable metadata.';

alter table public.profiles enable row level security;

-- Grants: be explicit, whatever the project's default privileges are.
revoke all on table public.profiles from anon, authenticated;
grant select on table public.profiles to authenticated;
-- Column-level grant: signed-in users can change their display name and nothing else.
-- In particular they can never change `role` or `id`.
grant update (display_name) on table public.profiles to authenticated;
grant select, insert, update, delete on table public.profiles to service_role;

-- Policies: a user sees and edits only their own row.
create policy "profiles_select_own"
  on public.profiles for select
  to authenticated
  using ((select auth.uid()) = id);

create policy "profiles_update_own"
  on public.profiles for update
  to authenticated
  using ((select auth.uid()) = id)
  with check ((select auth.uid()) = id);

-- Signup trigger. Kept minimal: if it fails, creating the user fails.
-- SECURITY DEFINER is needed to write to public.profiles from the auth schema, so the
-- function lives in the non-exposed `private` schema and is not callable by API roles.
-- The role is always the column default ('user'); it is never read from metadata.
create function private.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, email, display_name)
  values (new.id, new.email, new.raw_user_meta_data ->> 'display_name');
  return new;
end;
$$;

revoke all on function private.handle_new_user() from public, anon, authenticated;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function private.handle_new_user();
