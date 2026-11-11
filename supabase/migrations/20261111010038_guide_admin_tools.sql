-- Docs generator: global guides and the history of replaced guide files (owner, 2026-11-11).
--
--   * guides.is_global: the guide is a default of every draft, for every source (studies too).
--     Users may still untick it. While global, its source links are kept but unused.
--   * guide_revisions: when an admin replaces a guide's file, the OLD text is kept here (who,
--     when, file name). Drafts always use the guide's current text.
--
-- Everything is written by the server (service role). The browser has no access to guides or
-- their history (served through the admin API only).

alter table public.guides
  add column is_global boolean not null default false;

create index guides_global_idx on public.guides (id) where is_global and archived_at is null;

create table public.guide_revisions (
  id          bigint generated always as identity primary key,
  guide_id    bigint not null references public.guides (id) on delete restrict,
  filename    text not null default '',
  body_text   text not null check (length(body_text) > 0),
  sha256      text not null,
  -- The text was in use from `valid_from` until `replaced_at`, when `replaced_by` uploaded a new file.
  valid_from  timestamptz not null,
  replaced_by uuid references public.profiles (id) on delete set null,
  replaced_at timestamptz not null default now()
);

create index guide_revisions_guide_idx on public.guide_revisions (guide_id, replaced_at desc);
create index guide_revisions_replaced_by_idx on public.guide_revisions (replaced_by);

-- When the current text started: created_at for a guide never replaced.
alter table public.guides
  add column text_updated_at timestamptz;

alter table public.guide_revisions enable row level security;

-- The project's default privileges give service_role everything: revoke first, then grant
-- exactly what is needed. History rows are written once and never changed.
revoke all on table public.guide_revisions from public, anon, authenticated, service_role;
grant select, insert on table public.guide_revisions to service_role;

-- guide_revisions: no policy for the browser on purpose.

comment on column public.guides.is_global is
  'A default guide of every draft for every source; its source links are unused while true.';
comment on column public.guides.text_updated_at is
  'When the current text was uploaded as a replacement; null = the text from creation.';
comment on table public.guide_revisions is
  'Old texts of guides whose file an admin replaced. Written once; admin API only.';
