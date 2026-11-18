-- Own dates on a pipeline step (owner, 2026-11-18): a user adds dates with their own names
-- ("sponsor deadline", "site visit") next to the fixed ones. Kept as a small list on the step:
-- [{"id": "<short id>", "label": "<name>", "day": "YYYY-MM-DD" | null}], at most 10, checked by the
-- server. No new table: they are saved, audited and read with the step. Not yet in mails or the
-- calendar. The table keeps its grants: the server (service_role) is the only writer.

alter table public.pipeline_steps
  add column custom_dates jsonb not null default '[]'::jsonb;

alter table public.pipeline_steps
  add constraint pipeline_steps_custom_dates_is_list check (jsonb_typeof(custom_dates) = 'array');

comment on column public.pipeline_steps.custom_dates is
  'The step''s own dates: a list of {id, label, day}; at most 10, validated by the server.';
