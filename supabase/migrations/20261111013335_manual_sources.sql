-- Manual sources (owner, 2026-11-11; see docs/decisions.md "Manual sources").
-- A hospital without an e-IRB site: an admin uploads its documents by hand. Such a source is
-- never scanned. Its `url` is a placeholder the server makes ("manual:<random>"), unique like
-- every source url; its documents have an empty `source_url` (there is no page to link to).
-- Uploads are saved through public.commit_scan like a scan, so `last_scan_at` is the time of
-- the last upload. Nothing else in the schema changes.

alter table public.sources drop constraint sources_adapter_check;
alter table public.sources
  add constraint sources_adapter_check check (adapter in ('snuh_hrpp', 'rbay', 'manual'));
