-- Docs generator: form templates (owner, 2026-11-11).
--
--   * An admin uploads a hospital's blank form as DOCX. The server finds its fields: {{name}}
--     markers typed by the admin, tick-box groups (☐/□) and blanks it detects itself. The admin
--     reviews the list (status 'review'), then saves it ('ready'); only ready templates are used.
--   * A template is global (every site) or linked to sites, like guides. Archived, never deleted.
--   * A user may instead attach a one-off DOCX to one draft (stored under drafts/ in the bucket,
--     deleted with the draft).
--   * A draft made with a template keeps a copy of the template's fields and file path
--     (drafts.template), so replacing or archiving the template later changes nothing for it.
--     Its versions store one answer per field (draft_versions.fields); markdown keeps a readable
--     copy. A replaced template file is kept in the bucket, because drafts point to it.
--
-- Everything is written by the server (service role). The browser reads only its own drafts;
-- templates and their files have no browser access (served through the API).

create table public.form_templates (
  id           bigint generated always as identity primary key,
  title        text not null check (length(title) between 1 and 200),
  doc_type     text not null default '' check (length(doc_type) <= 100),
  filename     text not null,
  storage_path text not null,
  sha256       text not null,
  is_global    boolean not null default false,
  status       text not null default 'review' check (status in ('review', 'ready')),
  -- [{"key", "label", "kind": "text"|"choice", "options": [...], "multi", "source":
  --   "marker"|"auto", "where", "hint"}]
  fields       jsonb not null default '[]'::jsonb,
  created_by   uuid references public.profiles (id) on delete set null,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  archived_at  timestamptz
);

create index form_templates_created_by_idx on public.form_templates (created_by);
create index form_templates_global_idx on public.form_templates (id)
  where is_global and archived_at is null;

create table public.source_form_templates (
  source_id   bigint not null references public.sources (id) on delete cascade,
  template_id bigint not null references public.form_templates (id) on delete restrict,
  created_at  timestamptz not null default now(),
  primary key (source_id, template_id)
);

create index source_form_templates_template_idx on public.source_form_templates (template_id);

-- {"template_id" (null for a one-off file), "title", "filename", "storage_path", "fields": [...]}
alter table public.drafts
  add column template jsonb;

-- {"<field key>": "text" | ["picked option", ...]}; null for drafts without a template.
alter table public.draft_versions
  add column fields jsonb;

alter table public.form_templates        enable row level security;
alter table public.source_form_templates enable row level security;

-- The project's default privileges give service_role everything: revoke first, then grant
-- exactly what is needed. Templates are archived (update), never deleted; links can be removed.
revoke all on table public.form_templates, public.source_form_templates
  from public, anon, authenticated, service_role;
grant select, insert, update on table public.form_templates to service_role;
grant select, insert, delete on table public.source_form_templates to service_role;

-- form_templates and source_form_templates: no policy for the browser on purpose.
-- drafts / draft_versions keep their table-level grants and own-row read policies; they cover
-- the new columns.

-- Private bucket for template files. No storage.objects policies on purpose: with RLS on and no
-- policy, only the service role can read or write objects.
insert into storage.buckets (id, name, public, file_size_limit)
values ('form-templates', 'form-templates', false, 5000000)
on conflict (id) do nothing;

comment on table public.form_templates is
  'A hospital''s blank form (DOCX) with its fields, for the docs generator. Archived, never deleted.';
comment on table public.source_form_templates is 'Form templates a source offers.';
comment on column public.drafts.template is
  'The form template this draft fills (a copy made at creation); null = a Markdown draft.';
comment on column public.draft_versions.fields is
  'Template drafts only: one answer per field key (text, or a list of picked options).';
