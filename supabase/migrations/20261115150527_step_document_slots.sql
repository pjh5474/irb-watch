-- IRB pipeline phase C1: document lists and slots (owner, 2026-11-16; see docs/decisions.md
-- "IRB pipeline phase C1: document lists and slots" and docs/pipeline-design.md 4.3, 4.4).
--
--   * step_type_documents: the documents a step type needs, a global list and optional
--     per-hospital lists (server-wide, platform admins edit them, like step types). A hospital
--     has its own list for a step type once it has rows for it (even all archived = "none");
--     deleting its rows sends it back to the global list.
--   * step_documents becomes the slot: kind (pointer to the list row it was copied from),
--     section, origin, required, status, position, note, updated_at. The server copies the
--     slots when a step is made; later list edits do not change existing steps.
--   * step_types.required_documents is copied into the global lists and no longer read by the
--     server (the column is dropped in a later migration).
--   * Seeds (owner confirmed): 초기심의 and 변경심의 lists.
--   * Existing data (owner: pipeline data will be reset, only keep it working): documents get
--     a status (in_progress when they have a file) and are linked to a list row of the same
--     name; open, not-yet-submitted steps get the slots of their type's global list.
--   * Server only, as the other pipeline tables.
-- Existing step types are matched by name with spaces removed (the admin renamed the seeds).

-- 1. Document lists ----------------------------------------------------------------------------

create table public.step_type_documents (
  id            bigint generated always as identity primary key,
  step_type_id  bigint not null references public.step_types (id) on delete cascade,
  hospital_id   bigint references public.hospitals (id) on delete cascade,  -- null: the global list
  name          text not null check (length(btrim(name)) between 1 and 200),
  section       text not null check (section in ('application', 'submission', 'received', 'reference')),
  origin        text not null check (origin in ('draft', 'upload', 'library', 'received')),
  required      boolean not null default false,
  position      integer not null,
  archived_at   timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  updated_by    uuid references public.profiles (id) on delete set null
);

create unique index step_type_documents_live_name_key
  on public.step_type_documents (step_type_id, coalesce(hospital_id, 0), lower(btrim(name)))
  where archived_at is null;
create index step_type_documents_hospital_idx on public.step_type_documents (hospital_id);
create index step_type_documents_updated_by_idx on public.step_type_documents (updated_by);

alter table public.step_type_documents enable row level security;
revoke all on table public.step_type_documents from public, anon, authenticated, service_role;
grant select, insert, update, delete on table public.step_type_documents to service_role;  -- delete: back to global
comment on table public.step_type_documents is
  'Documents a step type needs: the global list (hospital_id null) and per-hospital lists (server-wide, platform admins).';

-- The old checklist names become the global lists (required uploads).
insert into public.step_type_documents (step_type_id, name, section, origin, required, position)
select t.id, btrim(d.name), 'submission', 'upload', true, d.n
from public.step_types t
cross join lateral unnest(t.required_documents) with ordinality as d(name, n)
where length(btrim(d.name)) between 1 and 200
on conflict do nothing;

-- Seeds for 초기심의 and 변경심의 (owner confirmed 2026-11-16), after any copied names.
insert into public.step_type_documents (step_type_id, name, section, origin, required, position)
select t.id, s.name, s.section, s.origin, s.required,
       coalesce((select max(position) from public.step_type_documents x
                 where x.step_type_id = t.id and x.hospital_id is null), 0) + s.n
from public.step_types t
join (values
  ('초기심의', 1,  '신청서',                              'application', 'draft',    true),
  ('초기심의', 2,  '접수증',                              'received',    'received', false),
  ('초기심의', 3,  '통지서',                              'received',    'received', false),
  ('초기심의', 4,  '연구계획서 (Protocol)',               'submission',  'library',  true),
  ('초기심의', 5,  '증례기록서 (CRF)',                    'submission',  'library',  true),
  ('초기심의', 6,  '연구대상자 설명문 및 동의서 (ICF)',   'submission',  'upload',   true),
  ('초기심의', 7,  '연구자 이력서 (CV) 및 GCP 이수증',    'submission',  'upload',   true),
  ('초기심의', 8,  '연구진 Delegation Log',               'submission',  'upload',   false),
  ('초기심의', 9,  '제품설명서',                          'submission',  'library',  false),
  ('초기심의', 10, '허가증',                              'submission',  'library',  false),
  ('초기심의', 11, '취약한 대상자 보호대책',              'submission',  'draft',    false),
  ('초기심의', 12, '연구대상자 모집문건 / 미제출사유서',  'submission',  'draft',    false),
  ('변경심의', 1,  '신청서',                              'application', 'draft',    true),
  ('변경심의', 2,  '접수증',                              'received',    'received', false),
  ('변경심의', 3,  '통지서',                              'received',    'received', false),
  ('변경심의', 4,  '변경대비표',                          'submission',  'draft',    true),
  ('변경심의', 5,  '변경된 문서 (새 버전)',               'submission',  'upload',   true),
  ('변경심의', 6,  '연구비 변경대비표',                   'submission',  'draft',    false),
  ('변경심의', 7,  '연구담당자 (CRA) 변경대비표',         'submission',  'draft',    false)
) as s(type_name, n, name, section, origin, required)
  on replace(t.name, ' ', '') = s.type_name
where t.archived_at is null
on conflict do nothing;

-- 2. Slots ---------------------------------------------------------------------------------------

alter table public.step_documents
  add column kind_id    bigint references public.step_type_documents (id) on delete set null,
  add column section    text not null default 'submission'
    check (section in ('application', 'submission', 'received', 'reference')),
  add column origin     text not null default 'upload'
    check (origin in ('draft', 'upload', 'library', 'received')),
  add column required   boolean not null default false,
  add column status     text not null default 'needed'
    check (status in ('needed', 'in_progress', 'final', 'not_needed')),
  add column position   integer not null default 0,
  add column note       text not null default '' check (length(note) <= 2000),
  add column updated_at timestamptz not null default now();

create index step_documents_kind_idx on public.step_documents (kind_id);

-- Existing documents: a file means in progress; the order follows the name.
update public.step_documents d
set status = 'in_progress'
where exists (select 1 from public.step_document_versions v where v.document_id = d.id);

update public.step_documents d
set position = o.n
from (
  select id, row_number() over (partition by step_id order by lower(name), id) as n
  from public.step_documents
) o
where o.id = d.id;

-- Existing documents named like a list row of their step's type take its section and flags.
update public.step_documents d
set kind_id = k.id, section = k.section, origin = k.origin, required = k.required
from public.pipeline_steps s, public.step_type_documents k
where d.step_id = s.id
  and k.step_type_id = s.step_type_id and k.hospital_id is null and k.archived_at is null
  and lower(btrim(k.name)) = lower(btrim(d.name));

-- Open, not-yet-submitted live steps get the missing slots of their type's global list
-- (no hospital has its own list yet).
insert into public.step_documents (step_id, name, kind_id, section, origin, required, status, position)
select s.id, k.name, k.id, k.section, k.origin, k.required, 'needed',
       coalesce((select max(position) from public.step_documents x where x.step_id = s.id), 0)
         + row_number() over (partition by s.id order by k.position, k.id)
from public.pipeline_steps s
join public.pipelines p on p.id = s.pipeline_id
join public.step_statuses st on st.key = s.status_key
join public.step_type_documents k
  on k.step_type_id = s.step_type_id and k.hospital_id is null and k.archived_at is null
where s.deleted_at is null and p.deleted_at is null
  and not st.closed and s.status_key <> 'submitted' and s.submitted_on is null
  and not exists (
    select 1 from public.step_documents d
    where d.step_id = s.id and d.deleted_at is null and lower(btrim(d.name)) = lower(btrim(k.name))
  )
on conflict do nothing;

-- 3. Functions -----------------------------------------------------------------------------------

-- Saves one whole list (a step type's global list, or one hospital's) in one statement: the
-- rows sent, in order, are the live list; a row with an id of this list is kept (renamed or
-- changed), others are added, and live rows not sent are archived. Rows are archived first,
-- so names may move between rows. The server checks the rows (names, limits, duplicates).
create function public.save_step_type_documents(
  p_step_type_id bigint, p_hospital_id bigint, p_rows jsonb, p_actor uuid
)
returns void
language plpgsql
security invoker
set search_path = ''
as $$
declare
  r jsonb;
  n integer := 0;
  row_id bigint;
begin
  update public.step_type_documents
  set archived_at = now(), updated_at = now(), updated_by = p_actor
  where step_type_id = p_step_type_id
    and hospital_id is not distinct from p_hospital_id
    and archived_at is null;
  for r in select value from jsonb_array_elements(p_rows) loop
    n := n + 1;
    row_id := nullif(r->>'id', '')::bigint;
    if row_id is not null then
      update public.step_type_documents
      set name = r->>'name', section = r->>'section', origin = r->>'origin',
          required = (r->>'required')::boolean, position = n, archived_at = null,
          updated_at = now(), updated_by = p_actor
      where id = row_id and step_type_id = p_step_type_id
        and hospital_id is not distinct from p_hospital_id;
      if not found then
        raise exception 'a document row is not in this list' using errcode = 'P0001';
      end if;
    else
      insert into public.step_type_documents
        (step_type_id, hospital_id, name, section, origin, required, position, updated_by)
      values (p_step_type_id, p_hospital_id, r->>'name', r->>'section', r->>'origin',
              (r->>'required')::boolean, n, p_actor);
    end if;
  end loop;
end;
$$;

revoke all on function public.save_step_type_documents(bigint, bigint, jsonb, uuid) from public, anon, authenticated;
grant execute on function public.save_step_type_documents(bigint, bigint, jsonb, uuid) to service_role;

-- Reorders a step's live slots in one statement (all or nothing), like reorder_pipeline_steps.
create function public.reorder_step_documents(p_step_id bigint, p_document_ids bigint[])
returns void
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if cardinality(p_document_ids) <> (select count(distinct x) from unnest(p_document_ids) x)
     or (select coalesce(array_agg(id order by id), '{}') from public.step_documents
         where step_id = p_step_id and deleted_at is null)
        <> (select coalesce(array_agg(x order by x), '{}') from unnest(p_document_ids) x) then
    raise exception 'the document list does not match the step''s documents' using errcode = 'P0001';
  end if;
  update public.step_documents d
  set position = o.n, updated_at = now()
  from unnest(p_document_ids) with ordinality as o(id, n)
  where d.id = o.id and d.step_id = p_step_id;
end;
$$;

revoke all on function public.reorder_step_documents(bigint, bigint[]) from public, anon, authenticated;
grant execute on function public.reorder_step_documents(bigint, bigint[]) to service_role;

comment on column public.step_types.required_documents is
  'No longer read (phase C1): copied into step_type_documents; dropped in a later migration.';
