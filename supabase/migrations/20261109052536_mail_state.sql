-- State for the email alarms and reports (see docs/decisions.md, "Email notifications").
--
--   source_alert_state  what an admin was last told about each source, so a problem is mailed
--                       once when it starts and once when it ends, never on every scan.
--   mail_claims         one row per mail that may be sent only once (for example the daily
--                       report of one day): the server claims the key, then sends; a failed
--                       send releases the claim.
--
-- Both tables are written and read only by the server (service role). Users have no access.

create table public.source_alert_state (
  source_id       bigint primary key references public.sources (id) on delete cascade,
  notified_level  text not null check (notified_level in ('attention', 'problem')),
  notified_reason text,
  notified_at     timestamptz not null default now()
);

alter table public.source_alert_state enable row level security;

-- The project's default privileges give service_role everything: revoke first, then grant
-- exactly what is needed.
revoke all on table public.source_alert_state from public, anon, authenticated, service_role;
grant select, insert, update, delete on table public.source_alert_state to service_role;

comment on table public.source_alert_state is
  'The alarm level an admin was last mailed for a source. No row = the source is not in an alarm state.';

create table public.mail_claims (
  key        text primary key,
  created_at timestamptz not null default now()
);

alter table public.mail_claims enable row level security;

revoke all on table public.mail_claims from public, anon, authenticated, service_role;
grant select, insert, delete on table public.mail_claims to service_role;

comment on table public.mail_claims is
  'Keys of mails that must be sent at most once (for example daily:2026-11-09).';
