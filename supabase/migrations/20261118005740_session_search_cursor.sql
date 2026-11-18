-- The daily AI search for IRB dates, slice 2 (owner, 2026-11-18; docs/decisions.md "IRB date
-- recommendations for users").
--
-- One row per hospital: where its daily search stands. After the daily scan the server reads a
-- hospital's notice board posts that are new or changed since `last_ok_at` and asks the AI for
-- meetings; `last_ok_at` moves only after a search that worked, so a failed day is read again
-- the next day. A failure keeps its reason here (`last_error`), never "no change".
-- Server only, like the other session tables. Platform-wide, like hospitals and sessions.

create table public.hospital_session_search (
  hospital_id   bigint primary key references public.hospitals (id) on delete cascade,
  last_run_at   timestamptz,                         -- the last daily search (worked or not)
  last_ok_at    timestamptz,                         -- the cursor: posts changed after it are not read yet
  last_status   text check (last_status in ('ok', 'failed')),
  last_error    text check (last_error is null or length(last_error) <= 1000),
  last_posts    integer not null default 0 check (last_posts >= 0),
  last_proposed integer not null default 0 check (last_proposed >= 0),
  updated_at    timestamptz not null default now()
);

alter table public.hospital_session_search enable row level security;
revoke all on table public.hospital_session_search from public, anon, authenticated, service_role;
grant select, insert, update on table public.hospital_session_search to service_role;
comment on table public.hospital_session_search is
  'Per hospital: how far the daily AI search for IRB dates has read (cursor) and why the last one failed. Server only.';
