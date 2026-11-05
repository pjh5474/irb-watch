-- AI results on each version, and the log that limits on-demand summaries per user.
--   category          what kind of document (set by Jev; free text so the list can change)
--   summary_source    ai | template (plain description without AI) | none (nothing available)
--   classification    Jev's answers and notes for audit; never document text
--   document_summary  summary of the WHOLE document requested by a user; cached here
--   (document_versions.summary and .priority already exist: the summary of the change)

alter table public.document_versions
  add column category text,
  add column summary_source text check (summary_source in ('ai', 'template', 'none')),
  add column classification jsonb,
  add column document_summary text,
  add column document_summary_at timestamptz;

-- One row per summary a user caused to be generated (cached answers are not logged).
-- Used for the per-user daily limit. Written and read only by the server.
create table public.summary_requests (
  id          bigint generated always as identity primary key,
  user_id     uuid not null references public.profiles (id) on delete cascade,
  version_id  bigint not null references public.document_versions (id) on delete cascade,
  created_at  timestamptz not null default now()
);

create index summary_requests_user_created_idx
  on public.summary_requests (user_id, created_at desc);
create index summary_requests_version_idx on public.summary_requests (version_id);

alter table public.summary_requests enable row level security;

revoke all on table public.summary_requests from anon, authenticated, service_role;
grant select, insert on table public.summary_requests to service_role;
grant usage on all sequences in schema public to service_role;
