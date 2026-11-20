-- A user's own R-BAY login (owner, 2026-11-20; see docs/decisions.md "User's own R-BAY login").
--
-- One row per (user, site). Today the only site is 'rbay-account': the person's R-BAY account,
-- one login for every hospital. The password is stored encrypted (ciphertext only; the plaintext
-- also repeats user_id and site, so a copied ciphertext is refused). It is used only for actions
-- the person starts (a login test, later IRB jobs such as a new-plan draft); never by the
-- scheduler, never by a scan. It is separate from site_credentials (the platform's own logins
-- for scans, admin-managed) so the admin API and the scans can never reach it.
--
-- Deleting the login clears ciphertext and login_hint but keeps the row, so the test counters
-- (last_tested_at, test_window_start, test_count) cannot be reset by saving and deleting again.
-- Deleting the account removes the row (on delete cascade).
--
-- Server only: nothing is granted to the browser; the page reads the status through the API.
-- Shaped so an organization id can be added later without changing the feature.

create table public.user_site_credentials (
  user_id           uuid not null references public.profiles (id) on delete cascade,
  site              text not null check (site in ('rbay-account')),
  ciphertext        text check (ciphertext is null or length(ciphertext) <= 4000),
  login_hint        text check (login_hint is null or length(login_hint) <= 40),
  status            text not null default 'untested'
                      check (status in ('untested', 'testing', 'ok', 'rejected', 'failed')),
  last_error        text check (last_error is null or length(last_error) <= 500),
  last_tested_at    timestamptz,
  last_ok_at        timestamptz,
  test_window_start timestamptz,
  test_count        integer not null default 0 check (test_count >= 0),
  -- R-BAY locks an account after 5 failed logins (its own notice, seen 2026-11-20), so the server
  -- stops after 3 rejected passwords in a row; a successful login sets it back to 0.
  reject_count      integer not null default 0 check (reject_count >= 0),
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  primary key (user_id, site),
  -- no secret, no hint, no state: a cleared row is 'untested' with nothing saved
  check (ciphertext is not null or (login_hint is null and status = 'untested'))
);

alter table public.user_site_credentials enable row level security;

revoke all on table public.user_site_credentials from public, anon, authenticated, service_role;
grant select, insert, update, delete on table public.user_site_credentials to service_role;

-- One refused password, counted in the database so two failures at the same moment (a job and a test)
-- both count: R-BAY locks the account at 5, and an undercount would let the server go on too long.
-- p_tested_at is the start time of the test that produced the answer; a stale test (the login was
-- deleted and saved again meanwhile) changes nothing. Null = a job's refusal, always counted.
create function public.user_login_rejected(
  p_user uuid, p_site text, p_reason text, p_tested_at timestamptz
) returns boolean
language plpgsql
set search_path = ''
as $$
declare
  n integer;
begin
  update public.user_site_credentials
     set status = 'rejected',
         last_error = left(p_reason, 300),
         reject_count = reject_count + 1,
         last_tested_at = case when p_tested_at is null then now() else last_tested_at end,
         updated_at = now()
   where user_id = p_user and site = p_site and ciphertext is not null
     and (p_tested_at is null or last_tested_at = p_tested_at);
  get diagnostics n = row_count;
  return n > 0;
end;
$$;

revoke all on function public.user_login_rejected(uuid, text, text, timestamptz) from public, anon, authenticated;
grant execute on function public.user_login_rejected(uuid, text, text, timestamptz) to service_role;

comment on table public.user_site_credentials is
  'A user''s own login for a site (R-BAY account), encrypted. Used only for actions the user starts, never by the scheduler. Server only; no browser grant.';
comment on column public.user_site_credentials.ciphertext is 'Encrypted JSON (owner = user id, site, id, password). Null when the user deleted the login.';
comment on column public.user_site_credentials.status is 'untested, testing (a login test is running), ok, rejected (R-BAY refused the password; no job may use it until a new one is saved), failed (the site could not be reached or changed; not the password''s fault).';
comment on column public.user_site_credentials.reject_count is 'Passwords R-BAY refused in a row (R-BAY locks the account at 5). At 3 the server tries nothing for 24 hours after the last one.';
comment on column public.user_site_credentials.test_count is 'Login tests started since test_window_start (a 24 hour window); the rate limit reads it.';
