-- IRB step reminders in the daily digest (IRB pipeline redesign, phase A2; see docs/decisions.md
-- "IRB pipeline redesign, phase A2").
--
--   step_reminders_enabled   the user's own choice, separate from digest_enabled (the document
--                            news). No row at all = both ON (the default).
--
-- authenticated has column-level grants only (see 20261109072825_notification_prefs.sql), and
-- those do not cover a new column: grant exactly this one for insert and update. service_role
-- has a table-level grant, which covers it already. The existing row policies (own row only)
-- apply unchanged.

alter table public.notification_prefs
  add column step_reminders_enabled boolean not null default true;

-- A user who already switched the daily mail off (in Settings or by the unsubscribe link) asked
-- for no mail: reminders start off for them too. Everyone else (and every new row) starts on.
update public.notification_prefs
  set step_reminders_enabled = digest_enabled
  where digest_enabled = false;

grant insert (step_reminders_enabled) on table public.notification_prefs to authenticated;
grant update (step_reminders_enabled) on table public.notification_prefs to authenticated;

comment on column public.notification_prefs.step_reminders_enabled is
  'IRB step due-date reminders in the daily mail. Default on; separate from digest_enabled.';
