-- File warnings an admin has reviewed, and attachments skipped for being too large
-- (see docs/decisions.md, "File warnings and big files").
--
--   documents.review_acknowledged_reason  the exact file-warning text an admin marked OK.
--       While a scan finds the same text, the document stays `current` (no new warning);
--       a different text puts it back into needs_review. Written by the server only.
--   documents.review_acknowledged_by / _at  who marked it OK and when (also in the audit log).
--   document_version_files.hash_only_reason gains 'too_large': an attachment over the size
--       limit is listed by name and size but not downloaded; the rest of the post is kept.
--
-- Readers of a document can see these columns like the others on `documents` (the table's
-- existing select grant and policies); the server is the only writer (service role).

alter table public.documents
  add column review_acknowledged_reason text,
  add column review_acknowledged_by uuid references public.profiles (id) on delete set null,
  add column review_acknowledged_at timestamptz;

create index documents_review_acknowledged_by_idx on public.documents (review_acknowledged_by);

alter table public.document_version_files
  drop constraint document_version_files_hash_only_reason_check;
alter table public.document_version_files
  add constraint document_version_files_hash_only_reason_check
  check (hash_only_reason in (
    'no_text_layer', 'unsupported_format', 'distribution_protected', 'unreadable', 'too_large'
  ));
