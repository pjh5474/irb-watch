-- R-BAY e-IRB boards (see docs/decisions.md, "R-BAY boards").
-- A second adapter next to snuh_hrpp. An R-BAY source always has a login (members-only); the
-- server refuses to create one without it.

alter table public.sources drop constraint sources_adapter_check;
alter table public.sources
  add constraint sources_adapter_check check (adapter in ('snuh_hrpp', 'rbay'));
