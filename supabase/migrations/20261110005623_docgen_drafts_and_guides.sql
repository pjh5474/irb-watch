-- Docs generator: the guide library (per source) and each user's drafts.
--
-- Owner decisions 2026-11-10:
--   * An admin keeps a library of guides (text extracted from PDF/HWP/HWPX/DOCX/MD/TXT) and links
--     each guide to one or more sources. A user generating from a source gets its guides by
--     default, may detach them and may attach extra guide files for one draft.
--   * A draft is Markdown, written by the AI from stored document versions, edited by the user
--     (by hand or by asking the AI to revise). Every change is a new draft_versions row.
--   * Drafts are deleted 30 days after their last change (done by the server, daily).
--   * Guides are archived, never deleted, so an old draft still names the guide it used.
--
-- Everything is written by the server (service role). The browser may only read its own drafts.
-- Guides have no browser access: they are served through the API, which checks source access.

-- ---------------------------------------------------------------------------
-- guides
-- ---------------------------------------------------------------------------
create table public.guides (
  id          bigint generated always as identity primary key,
  title       text not null check (length(title) between 1 and 200),
  filename    text not null default '',
  body_text   text not null check (length(body_text) > 0),
  sha256      text not null,
  created_by  uuid references public.profiles (id) on delete set null,
  created_at  timestamptz not null default now(),
  archived_at timestamptz
);

create index guides_created_by_idx on public.guides (created_by);

-- ---------------------------------------------------------------------------
-- source_guides: which guides a source uses by default
-- ---------------------------------------------------------------------------
create table public.source_guides (
  source_id  bigint not null references public.sources (id) on delete cascade,
  guide_id   bigint not null references public.guides (id) on delete restrict,
  created_at timestamptz not null default now(),
  primary key (source_id, guide_id)
);

create index source_guides_guide_idx on public.source_guides (guide_id);

-- ---------------------------------------------------------------------------
-- drafts
--   document_refs: [{"document_id": 1, "version_no": 2, "source_id": 3}, ...]
--   guide_ids:     library guides used (copied at creation, so later relinking changes nothing)
--   extra_guides:  [{"filename": "...", "text": "..."}] one-off guides the user attached
--   ai_override_source_ids: AI-off sources the user ticked the warning box for
-- ---------------------------------------------------------------------------
create table public.drafts (
  id                     uuid primary key default gen_random_uuid(),
  user_id                uuid not null references public.profiles (id) on delete cascade,
  title                  text not null default '',
  instruction            text not null default '',
  document_refs          jsonb not null default '[]'::jsonb,
  guide_ids              bigint[] not null default '{}',
  extra_guides           jsonb not null default '[]'::jsonb,
  ai_override_source_ids bigint[] not null default '{}',
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now()
);

create index drafts_user_idx on public.drafts (user_id, updated_at desc);
create index drafts_updated_idx on public.drafts (updated_at);

-- ---------------------------------------------------------------------------
-- draft_versions
--   user_id repeats the owner so the read policy needs no join.
-- ---------------------------------------------------------------------------
create table public.draft_versions (
  id          bigint generated always as identity primary key,
  draft_id    uuid not null references public.drafts (id) on delete cascade,
  user_id     uuid not null references public.profiles (id) on delete cascade,
  version_no  integer not null check (version_no >= 1),
  markdown    text not null,
  origin      text not null check (origin in ('ai', 'user_edit', 'ai_revision')),
  instruction text not null default '',
  created_at  timestamptz not null default now(),
  unique (draft_id, version_no)
);

create index draft_versions_user_idx on public.draft_versions (user_id);

-- ---------------------------------------------------------------------------
-- Row level security and grants
-- The project's default privileges give service_role everything: revoke first, then grant
-- exactly what is needed.
-- ---------------------------------------------------------------------------
alter table public.guides         enable row level security;
alter table public.source_guides  enable row level security;
alter table public.drafts         enable row level security;
alter table public.draft_versions enable row level security;

revoke all on table
  public.guides, public.source_guides, public.drafts, public.draft_versions
from public, anon, authenticated, service_role;

grant select on table public.drafts, public.draft_versions to authenticated;

-- Guides are archived (update), never deleted. Links can be removed.
grant select, insert, update on table public.guides to service_role;
grant select, insert, delete on table public.source_guides to service_role;
grant select, insert, update, delete on table public.drafts to service_role;
-- Versions are written once and never edited; they go with their draft (cascade).
grant select, insert on table public.draft_versions to service_role;

create policy "drafts_select_own"
  on public.drafts for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy "draft_versions_select_own"
  on public.draft_versions for select
  to authenticated
  using ((select auth.uid()) = user_id);

-- guides and source_guides: no policy for the browser on purpose.

comment on table public.guides is
  'Admin-kept guide for writing a submission form; text only. Archived, never deleted.';
comment on table public.source_guides is 'Default guides of a source.';
comment on table public.drafts is
  'One draft of a user. Deleted by the server 30 days after updated_at.';
comment on table public.draft_versions is
  'Every state of a draft: AI first draft, hand edits, AI revisions.';
