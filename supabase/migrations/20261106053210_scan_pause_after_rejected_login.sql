-- Scheduled scans must not retry a rejected login (it could lock the hospital account).
-- After a rejected login (wrong password, no stored credentials) the server writes the
-- reason here; the scheduler skips the source until a person starts a scan of it.
-- Every finished scan overwrites this column: a successful one clears it.

alter table public.sources add column scans_paused_reason text;

comment on column public.sources.scans_paused_reason is
  'Why scheduled scans skip this source (e.g. a rejected login). Null = not paused. A manual scan clears it on success and sets it again on a new rejection.';
