-- Docs generator: what each AI version of a draft was written from (owner, 2026-11-11).
--
-- inputs: {"documents": [{"document_id", "version_no", "source_id", "files" (names, or null
--          for every file), "page_text", "compare_with" (null = the version just before),
--          "compare"}], "guide_ids": [...], "extra_guides": [file names only],
--          "ai_override_source_ids": [...]}
-- Null for hand edits and for versions saved before this column existed.
--
-- The documents' new keys (files, page_text, compare_with, compare) also go into
-- drafts.document_refs, which is jsonb already; old rows without them mean "every file, page
-- text, compare with the version just before".
--
-- Grants: draft_versions has table-level grants (authenticated: select; service_role: select,
-- insert), which cover a new column, so nothing changes. The browser reads its own rows only
-- (draft_versions_select_own); versions are still never updated.

alter table public.draft_versions
  add column inputs jsonb;

comment on column public.draft_versions.inputs is
  'AI versions only: the documents (with chosen files and comparison), guide ids and attached guide file names this version was written from.';
