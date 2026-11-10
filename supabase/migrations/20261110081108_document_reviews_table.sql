-- File-warning approvals move to their own admin-only table (code review, 2026-11-10).
--
-- The previous migration put review_acknowledged_reason/_by/_at on `documents`, which every
-- reader of a document can select through supabase-js, so normal users could read which admin
-- approved what. Those columns were added the same day and hold no data; they are dropped.
--
-- document_reviews: one row per document whose file warnings an admin marked OK.
--   ack_key   the warning text plus the hashes of the flagged files. A scan whose warnings
--             produce the same key stays quiet; any other text or a replaced file asks again.
--   reason    the warning text as the admin saw it.
-- Only the server (service role) reads or writes it: no grants and no policies for users.

alter table public.documents
  drop column review_acknowledged_reason,
  drop column review_acknowledged_by,
  drop column review_acknowledged_at;

create table public.document_reviews (
  document_id     bigint primary key references public.documents (id) on delete cascade,
  ack_key         text not null,
  reason          text not null,
  acknowledged_by uuid references public.profiles (id) on delete set null,
  acknowledged_at timestamptz not null default now()
);

create index document_reviews_acknowledged_by_idx on public.document_reviews (acknowledged_by);

alter table public.document_reviews enable row level security;

-- The project's default privileges give service_role everything: revoke first, then grant
-- exactly what is needed. No grant at all for anon or authenticated.
revoke all on table public.document_reviews from public, anon, authenticated, service_role;
grant select, insert, update, delete on table public.document_reviews to service_role;

comment on table public.document_reviews is
  'File warnings an admin marked OK, per document. Server-only (no user access).';
