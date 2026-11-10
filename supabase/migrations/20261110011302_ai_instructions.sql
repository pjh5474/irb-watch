-- Admin-edited instructions for the AI features, with full history.
--
-- Owner decisions 2026-11-10:
--   * Each AI feature (the chat assistant, the docs generator, summaries) has a fixed safety
--     core in code. Admins may only ADD instructions after it (tone, terms, format); they
--     cannot remove the rules about untrusted text, links, HTML and read-only tools.
--   * Every save is a new row; the newest row of a feature is the one in use. An empty body
--     means "no added instructions" (reset to default). Restoring an old version copies it
--     into a new row (restored_from), so the history is never rewritten.
--   * At most 4,000 characters.
--
-- Only the server reads and writes it (service role); admins use it through the API.

create table public.ai_instructions (
  id            bigint generated always as identity primary key,
  feature       text not null check (feature in ('agent', 'docgen', 'summary')),
  body          text not null default '' check (length(body) <= 4000),
  created_by    uuid references public.profiles (id) on delete set null,
  created_at    timestamptz not null default now(),
  restored_from bigint references public.ai_instructions (id) on delete set null
);

create index ai_instructions_feature_idx on public.ai_instructions (feature, id desc);
create index ai_instructions_created_by_idx on public.ai_instructions (created_by);
create index ai_instructions_restored_from_idx on public.ai_instructions (restored_from);

alter table public.ai_instructions enable row level security;

revoke all on table public.ai_instructions from public, anon, authenticated, service_role;
-- Written once, never edited or deleted.
grant select, insert on table public.ai_instructions to service_role;

-- No policy for the browser on purpose.

comment on table public.ai_instructions is
  'Admin-added AI instructions per feature; newest row per feature is in use. Append-only.';
