-- Images in a post's text (owner, 2026-11-16; docs/decisions.md "Images in R-BAY posts").
--   document_version_files.hash_only_reason gains 'inline_image': an image shown inside the
--   post's text (not an attachment). It is stored and shown like a file, has no text, and is
--   never a warning. Without this value commit_scan would refuse a whole scan that has one.

alter table public.document_version_files
  drop constraint document_version_files_hash_only_reason_check;
alter table public.document_version_files
  add constraint document_version_files_hash_only_reason_check
  check (hash_only_reason in (
    'no_text_layer', 'unsupported_format', 'distribution_protected', 'unreadable', 'too_large',
    'inline_image'
  ));
