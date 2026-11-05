-- Show users WHY something is flagged or has no text.
--   documents.review_reason          why a document is in needs_review (null otherwise)
--   document_version_files.extract_note  human-readable reason a file has no extracted text
--   hash_only_reason gains 'distribution_protected' (HWP whose text is scrambled on purpose)
--   and 'unreadable' (a real file we could not read: corrupt, password-protected, or a bug).

alter table public.documents add column review_reason text;

alter table public.document_version_files add column extract_note text;

alter table public.document_version_files
  drop constraint document_version_files_hash_only_reason_check;
alter table public.document_version_files
  add constraint document_version_files_hash_only_reason_check
  check (hash_only_reason in (
    'no_text_layer', 'unsupported_format', 'distribution_protected', 'unreadable'
  ));
