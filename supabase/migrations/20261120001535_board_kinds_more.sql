-- Board kinds for the new R-BAY boards (owner, 2026-11-20): IRB위원 리스트 (committee_members),
-- 뉴스 (news), FAQ (faq). 기타 stays 'other'. Only the check on sources.board_kind changes; no
-- row changes, no new table, no grant changes. The list in code is services/hospitals.BOARD_KINDS.

alter table public.sources drop constraint sources_board_kind_check;
alter table public.sources
  add constraint sources_board_kind_check
  check (board_kind in ('notice', 'forms', 'regulations', 'committee_members', 'news', 'faq', 'other'));

comment on column public.sources.board_kind is
  'notice, forms, regulations, committee_members, news, faq or other. Sets defaults only (e.g. pipeline step inputs).';
