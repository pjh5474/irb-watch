-- What the change-detection logic needs beyond the first domain migration:
--   * a files table per version (hash, extracted text, storage path) replacing the files JSON list;
--   * change types 'new' (appeared after the first scan) and 'resaved' (bytes changed, text did not);
--   * documents.latest_version_id so a scan loads one version per document, not the history;
--   * sources.scan_started_at as a lock so two scans of one source cannot overlap.
-- The tables are still empty, so dropping the JSON column loses nothing.

-- ---------------------------------------------------------------------------
-- document_versions: new change types, no JSON file list
-- ---------------------------------------------------------------------------
alter table public.document_versions drop column files;

alter table public.document_versions drop constraint document_versions_change_type_check;
alter table public.document_versions
  add constraint document_versions_change_type_check
  check (change_type in ('baseline', 'new', 'updated', 'resaved'));

alter table public.document_versions
  add constraint document_versions_id_source_key unique (id, source_id);

-- ---------------------------------------------------------------------------
-- document_version_files: every file attached to a version
-- ---------------------------------------------------------------------------
create table public.document_version_files (
  id                bigint generated always as identity primary key,
  version_id        bigint not null,
  source_id         bigint not null,              -- denormalised so RLS needs no join
  filename          text not null,
  size_label        text,
  sha256            text not null,                -- hash of the file bytes
  storage_path      text,                         -- object name in the `documents` bucket
  text_content      text,                         -- normalized extracted text; null when hash-only
  hash_only_reason  text check (hash_only_reason in ('no_text_layer', 'unsupported_format')),
  unique (version_id, filename),
  foreign key (version_id, source_id)
    references public.document_versions (id, source_id) on delete cascade
);

create index document_version_files_source_idx on public.document_version_files (source_id);
-- Lets a scan ask "is this file already in storage?" without scanning the table.
create index document_version_files_stored_sha_idx
  on public.document_version_files (sha256) where storage_path is not null;

-- ---------------------------------------------------------------------------
-- documents.latest_version_id and sources.scan_started_at
-- ---------------------------------------------------------------------------
alter table public.documents
  add column latest_version_id bigint references public.document_versions (id) on delete set null;
create index documents_latest_version_idx on public.documents (latest_version_id);

alter table public.sources add column scan_started_at timestamptz;
comment on column public.sources.scan_started_at is
  'Set while a scan runs; a value older than 30 minutes is treated as a crashed scan.';

-- ---------------------------------------------------------------------------
-- RLS and grants for the new table (same model as document_versions)
-- ---------------------------------------------------------------------------
alter table public.document_version_files enable row level security;

revoke all on table public.document_version_files from anon, authenticated;
grant select on table public.document_version_files to authenticated;
revoke all on table public.document_version_files from service_role;
grant select, insert, update, delete on table public.document_version_files to service_role;
grant usage on all sequences in schema public to service_role;

create policy "document_version_files_select_subscribed_or_admin"
  on public.document_version_files for select
  to authenticated
  using (
    (select private.is_admin())
    or source_id in (
      select s.source_id from public.subscriptions s where s.user_id = (select auth.uid())
    )
  );
