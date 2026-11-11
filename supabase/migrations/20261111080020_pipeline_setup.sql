-- IRB pipeline, phase 1: the admin's setup (owner, 2026-11-11; see docs/decisions.md
-- "IRB pipeline: admin setup").
--
--   * step_statuses: the list of step statuses, seeded, edited by platform admins. A step stores
--     the status key; `closed` marks a finished step (no due-date reminder).
--   * step_types: the kinds of IRB step (initial review, amendment, ...), each with its own AI
--     settings: added instructions, default guides and form, what the AI reads by default, and a
--     required-documents checklist.
--   * pipeline_templates: the default step order. One global row (source_id null) and optional
--     per-hospital overrides. A new pipeline copies the matching one (phase 2).
--   * Server-wide settings (platform admins), not per organization, by the owner's choice; an
--     organization_id can be added later without changing the feature.
--   * Server only: RLS on, no browser access; the API serves them.

create table public.step_statuses (
  key         text primary key check (key ~ '^[a-z][a-z0-9_]{0,39}$'),
  label       text not null check (length(btrim(label)) between 1 and 60),
  closed      boolean not null default false,
  position    integer not null,
  archived_at timestamptz,
  created_at  timestamptz not null default now()
);

insert into public.step_statuses (key, label, closed, position) values
  ('not_started',         '시작 전',      false, 1),
  ('preparing',           '준비 중',      false, 2),
  ('submitted',           '제출됨',       false, 3),
  ('revisions_requested', '보완 요청',    false, 4),
  ('approved',            '승인',         true,  5),
  ('rejected',            '반려',         true,  6),
  ('not_needed',          '해당 없음',    true,  7);

create table public.step_types (
  id                    bigint generated always as identity primary key,
  name                  text not null check (length(btrim(name)) between 1 and 100),
  description           text not null default '' check (length(description) <= 1000),
  instructions          text not null default '' check (length(instructions) <= 4000),
  guide_ids             bigint[] not null default '{}',
  form_template_id      bigint references public.form_templates (id) on delete set null,
  input_earlier_steps   boolean not null default true,
  input_board_documents boolean not null default false,
  required_documents    text[] not null default '{}'
                          check (cardinality(required_documents) <= 50),
  archived_at           timestamptz,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  updated_by            uuid references public.profiles (id) on delete set null
);

create unique index step_types_live_name_key
  on public.step_types (lower(btrim(name)))
  where archived_at is null;
create index step_types_form_template_idx on public.step_types (form_template_id);
create index step_types_updated_by_idx on public.step_types (updated_by);

insert into public.step_types (name, description, input_earlier_steps, input_board_documents) values
  ('초기 심의', '연구 시작 전 IRB 최초 심의 신청', false, true),
  ('변경 심의', '승인된 연구의 변경(계획서, 동의서 등) 심의 신청', true, true),
  ('지속 심의', '승인 기간 연장을 위한 지속(중간) 심의', true, true),
  ('중대한 이상사례 보고', 'SAE 및 안전성 정보 보고', true, false),
  ('위반/이탈 보고', '계획서 위반 및 이탈 보고', true, false),
  ('종료 보고', '연구 종료 및 결과 보고', true, true);

create table public.pipeline_templates (
  id            bigint generated always as identity primary key,
  source_id     bigint references public.sources (id) on delete cascade,
  step_type_ids bigint[] not null check (cardinality(step_type_ids) between 1 and 30),
  updated_at    timestamptz not null default now(),
  updated_by    uuid references public.profiles (id) on delete set null
);

-- One global template (source_id null) and at most one per hospital.
create unique index pipeline_templates_source_key
  on public.pipeline_templates (source_id) where source_id is not null;
create unique index pipeline_templates_global_key
  on public.pipeline_templates ((source_id is null)) where source_id is null;
create index pipeline_templates_updated_by_idx on public.pipeline_templates (updated_by);

insert into public.pipeline_templates (source_id, step_type_ids)
select null, array_agg(id order by id)
from public.step_types
where name in ('초기 심의', '변경 심의', '지속 심의', '종료 보고');

alter table public.step_statuses      enable row level security;
alter table public.step_types         enable row level security;
alter table public.pipeline_templates enable row level security;

revoke all on table public.step_statuses, public.step_types, public.pipeline_templates
from public, anon, authenticated, service_role;
grant select, insert, update on table public.step_statuses, public.step_types to service_role;
grant select, insert, update, delete on table public.pipeline_templates to service_role;
-- No policy for the browser: only the server reads these tables.

comment on table public.step_statuses is
  'Step statuses of the IRB pipeline (admin-edited, server-wide). closed = finished step.';
comment on table public.step_types is
  'Kinds of IRB step with their AI settings and required-documents checklist (admin, server-wide).';
comment on table public.pipeline_templates is
  'Default step order: one global row (source_id null) and optional per-hospital rows.';
