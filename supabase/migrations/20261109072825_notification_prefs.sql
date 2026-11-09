-- Settings for the daily digest mail (see docs/decisions.md, "Email notifications", phase 2).
--
--   digest_enabled   the user's own choice. No row at all = the digest is ON (the default).
--   last_digest_at   written only by the server: the cursor of the last digest that was sent, so
--                    the next one covers everything since, and a failed day is not lost.
--
-- A signed-in user may read their own row, create it and change digest_enabled. Column-level
-- grants mean a user can never write last_digest_at or updated_at from the browser.

create table public.notification_prefs (
  user_id        uuid primary key references public.profiles (id) on delete cascade,
  digest_enabled boolean not null default true,
  last_digest_at timestamptz,
  updated_at     timestamptz not null default now()
);

alter table public.notification_prefs enable row level security;

-- The project's default privileges give service_role everything: revoke first, then grant
-- exactly what is needed.
revoke all on table public.notification_prefs from public, anon, authenticated, service_role;
grant select on table public.notification_prefs to authenticated;
grant insert (user_id, digest_enabled) on table public.notification_prefs to authenticated;
grant update (digest_enabled) on table public.notification_prefs to authenticated;
grant select, insert, update, delete on table public.notification_prefs to service_role;

create policy "notification_prefs_select_own"
  on public.notification_prefs for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy "notification_prefs_insert_own"
  on public.notification_prefs for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

create policy "notification_prefs_update_own"
  on public.notification_prefs for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

-- updated_at follows every change, including a user's own toggle (which cannot write it).
create function private.touch_notification_prefs()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

revoke all on function private.touch_notification_prefs() from public, anon, authenticated;

create trigger touch_notification_prefs
  before update on public.notification_prefs
  for each row execute function private.touch_notification_prefs();

comment on table public.notification_prefs is
  'Per-user mail settings. No row = daily digest on. last_digest_at is server-only.';
