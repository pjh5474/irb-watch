-- IRB pipeline phase C1, after the code review (owner, 2026-11-16; docs/decisions.md "IRB
-- pipeline phase C1: document lists and slots"):
--   * step_documents.listed: the slot was copied from a document list. It keeps the slot's
--     name and section locked and the slot cannot be removed (the user marks it not needed),
--     even when the list row is deleted later ("back to the global list" deletes a hospital's
--     rows, and kind_id is set null then).
-- Server only, as the other pipeline tables (the column follows the table's grants).

alter table public.step_documents
  add column listed boolean not null default false;

update public.step_documents set listed = true where kind_id is not null;

comment on column public.step_documents.listed is
  'Copied from a document list: name and section stay, the slot is not removed (phase C1).';
