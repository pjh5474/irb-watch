-- IRB pipeline redesign, phase B2b: steps (owner, 2026-11-14; see docs/decisions.md
-- "IRB pipeline redesign, phase B2b: steps" and docs/pipeline-design.md 4.2).
--
--   * step_results: the IRB's answer, an admin-edited list (server-wide, like step_statuses).
--     approved = counts as approved; needs_answer = the step page highlights "Add follow-up".
--   * pipeline_steps: parent_step_id (a follow-up answers that step; one level, same pipeline),
--     sequence_no ("2" in 2차 변경심의), subject ("PT 2.0, CRF 1.1"), result_key.
--   * step_types: category and numbered.
--   * Statuses tidied (owner): a closed status 완료 is added; 승인 and 반려 are archived (the
--     result says that now). 보완요청 stays a work state. Steps on 승인 / 반려 get the matching
--     result and move to 완료.
--   * Seeds (owner): six results; new step types 중간보고, 문제보고, 연구자보고, 결과보고 and the
--     follow-ups 행정수정답변, 보완/시정답변, 재심의. The global step order becomes 초기심의 only;
--     hospitals' own orders and existing pipelines are left as they are.
--   * Existing steps of numbered types are numbered 1, 2, ... per pipeline and type, in step order.
--   * Server only, as the other pipeline tables.
-- Existing step types are matched by name with spaces removed (the admin renamed the seeds).

-- 1. Results ------------------------------------------------------------------------------------

create table public.step_results (
  key          text primary key check (key ~ '^[a-z][a-z0-9_]{0,39}$'),
  label        text not null check (length(btrim(label)) between 1 and 60),
  approved     boolean not null default false,
  needs_answer boolean not null default false,
  position     integer not null,
  archived_at  timestamptz,
  created_at   timestamptz not null default now()
);

insert into public.step_results (key, label, approved, needs_answer, position) values
  ('approved',                '승인',          true,  false, 1),
  ('approved_after_revision', '수정 후 승인',  true,  false, 2),
  ('corrective_approval',     '시정승인',      true,  true,  3),
  ('administrative_revision', '행정수정 요청', false, true,  4),
  ('revise_and_resubmit',     '보완 후 재심의', false, true, 5),
  ('rejected',                '반려',          false, false, 6);

alter table public.step_results enable row level security;
revoke all on table public.step_results from public, anon, authenticated, service_role;
grant select, insert, update on table public.step_results to service_role;
comment on table public.step_results is
  'The IRB''s answer to a step (admin-edited, server-wide). approved: counts as approved; needs_answer: offer a follow-up step.';

-- 2. Step types: category and numbering --------------------------------------------------------

alter table public.step_types
  add column category text not null default 'other'
    check (category in ('initial', 'amendment', 'continuing', 'deviation', 'safety',
                        'report', 'closing', 'follow_up', 'other')),
  add column numbered boolean not null default false;

update public.step_types t
set category = m.category, numbered = m.numbered
from (values
  ('초기심의',           'initial',    false),
  ('변경심의',           'amendment',  true),
  ('지속심의',           'continuing', true),
  ('중대한이상사례보고', 'safety',     true),
  ('위반/이탈보고',      'deviation',  true),
  ('종료보고',           'closing',    false)
) as m(name, category, numbered)
where replace(t.name, ' ', '') = m.name;

-- New types, skipped when a live type of the same name (spaces ignored) exists.
insert into public.step_types (name, description, category, numbered, input_earlier_steps, input_board_documents)
select s.name, s.description, s.category, s.numbered, true, false
from (values
  ('중간 보고',      '연구 진행 상황 중간보고',                  'continuing', true),
  ('문제 보고',      '예상하지 못한 문제 보고',                  'report',     false),
  ('연구자 보고',    '연구자 변경 등 연구자 보고',               'report',     false),
  ('결과 보고',      '연구 결과 보고',                           'closing',    false),
  ('행정수정 답변',  'IRB 행정수정 요청에 대한 답변',            'follow_up',  true),
  ('보완/시정 답변', 'IRB 보완·시정 요청에 대한 답변',           'follow_up',  true),
  ('재심의',         '보완 후 재심의 신청',                      'follow_up',  true)
) as s(name, description, category, numbered)
where not exists (
  select 1 from public.step_types t
  where t.archived_at is null and replace(lower(btrim(t.name)), ' ', '') = replace(lower(s.name), ' ', '')
);

-- 3. Steps: follow-ups, numbers, subject, result -----------------------------------------------

-- Target of the parent's composite key: a follow-up is always in its parent's pipeline.
alter table public.pipeline_steps
  add constraint pipeline_steps_id_pipeline_key unique (id, pipeline_id);

alter table public.pipeline_steps
  add column parent_step_id bigint,
  add column sequence_no integer check (sequence_no between 1 and 999),
  add column subject text not null default '' check (length(subject) <= 200),
  add column result_key text references public.step_results (key);

-- Purging a parent clears only the link (the follow-up stays in the pipeline).
alter table public.pipeline_steps
  add constraint pipeline_steps_parent_fkey
  foreign key (parent_step_id, pipeline_id)
  references public.pipeline_steps (id, pipeline_id)
  on delete set null (parent_step_id);

create index pipeline_steps_parent_idx on public.pipeline_steps (parent_step_id);
create index pipeline_steps_result_idx on public.pipeline_steps (result_key);

-- One level: a follow-up's parent is a live main step, and a step with follow-ups cannot
-- become one. Also catches a race: a follow-up added while its main step is removed.
create function private.check_step_parent()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if new.parent_step_id is null then
    return new;
  end if;
  if new.parent_step_id = new.id then
    raise exception 'a step cannot answer itself' using errcode = 'P0001';
  end if;
  if exists (select 1 from public.pipeline_steps p where p.id = new.parent_step_id and p.parent_step_id is not null) then
    raise exception 'a follow-up step answers a main step, not another follow-up' using errcode = 'P0001';
  end if;
  if exists (select 1 from public.pipeline_steps p where p.id = new.parent_step_id and p.deleted_at is not null) then
    raise exception 'the step to answer was just removed' using errcode = 'P0001';
  end if;
  if exists (select 1 from public.pipeline_steps c where c.parent_step_id = new.id) then
    raise exception 'a step with follow-up steps cannot become a follow-up' using errcode = 'P0001';
  end if;
  return new;
end;
$$;

revoke all on function private.check_step_parent() from public, anon, authenticated;

create trigger pipeline_steps_parent_check
  before insert or update of parent_step_id on public.pipeline_steps
  for each row execute function private.check_step_parent();

-- The other side of the race: a main step is not removed while it has live follow-ups (the
-- service checks first and names them; this catches one added at the same moment).
create function private.check_step_removal()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if new.deleted_at is not null and old.deleted_at is null
     and exists (select 1 from public.pipeline_steps c where c.parent_step_id = new.id and c.deleted_at is null) then
    raise exception 'this step has follow-up steps; remove them first' using errcode = 'P0001';
  end if;
  return new;
end;
$$;

revoke all on function private.check_step_removal() from public, anon, authenticated;

create trigger pipeline_steps_removal_check
  before update of deleted_at on public.pipeline_steps
  for each row execute function private.check_step_removal();

-- 4. Statuses tidied ----------------------------------------------------------------------------

-- 완료 goes right after the last live open status, whatever order the admin has set; it is
-- skipped when the key exists or a live status is already called 완료 (labels are unique
-- among live statuses), and then that one is used.
do $$
declare
  v_done text;
  v_at integer;
begin
  select key into v_done from public.step_statuses
  where key = 'done' or (archived_at is null and lower(btrim(label)) = '완료')
  order by (key = 'done') desc limit 1;
  if v_done is null then
    select coalesce(max(position), 0) + 1 into v_at from public.step_statuses
    where archived_at is null and not closed;
    update public.step_statuses set position = position + 1 where position >= v_at;
    insert into public.step_statuses (key, label, closed, position) values ('done', '완료', true, v_at);
    v_done := 'done';
  end if;
  -- Steps on the old closed statuses keep the answer as a result and move to 완료.
  update public.pipeline_steps set result_key = 'approved', status_key = v_done
  where status_key = 'approved' and result_key is null;
  update public.pipeline_steps set result_key = 'rejected', status_key = v_done
  where status_key = 'rejected' and result_key is null;
end;
$$;

update public.step_statuses set archived_at = now()
where key in ('approved', 'rejected') and archived_at is null;

-- 5. Global order: 초기심의 only (D9). Hospitals' own orders are kept. -----------------------------

update public.pipeline_templates
set step_type_ids = array[(
      select t.id from public.step_types t
      where t.archived_at is null and t.category = 'initial'
      order by t.id limit 1
    )],
    updated_at = now()
where hospital_id is null
  and exists (select 1 from public.step_types t where t.archived_at is null and t.category = 'initial');

-- 6. Numbers for existing steps of numbered types ----------------------------------------------

update public.pipeline_steps s
set sequence_no = n.no
from (
  select st.id, row_number() over (partition by st.pipeline_id, st.step_type_id order by st.position, st.id) as no
  from public.pipeline_steps st
  join public.step_types t on t.id = st.step_type_id
  where t.numbered and st.deleted_at is null and st.parent_step_id is null
) n
where s.id = n.id and s.sequence_no is null;

comment on column public.pipeline_steps.parent_step_id is
  'Set on a follow-up step: the main step it answers (same pipeline, one level).';
comment on column public.pipeline_steps.sequence_no is
  'The round number shown as {n}차; filled by the server for numbered step types, editable.';
comment on column public.pipeline_steps.result_key is
  'The IRB''s answer (step_results); separate from the work status.';
