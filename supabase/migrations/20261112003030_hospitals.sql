-- Hospitals (owner, 2026-11-12; see docs/decisions.md "Hospitals").
--
--   * A hospital groups its boards (sources). An R-BAY hospital has up to 3 boards (공지, 양식함,
--     관련규정); before this, each board stood in for "the hospital".
--   * Platform-wide catalog edited by the platform admin (the server). Organizations never own a
--     hospital. Every signed-in user may read the list (names only): any hospital may be added
--     to a project and get a pipeline. Board documents stay gated per board (can_read_source).
--   * sources.hospital_id (required) and sources.board_kind (fixed list in code: notice, forms,
--     regulations, other). The kind only sets defaults; users still pick documents per step.
--   * Moved from board to hospital: projects (project_sources -> project_hospitals), pipelines,
--     per-hospital step orders (pipeline_templates), default guides (source_guides ->
--     hospital_guides) and form templates (source_form_templates -> hospital_form_templates).
--   * Backfill: R-BAY boards grouped by agency code (hospital name = text before ' : '), all
--     SNUH HRPP boards -> 서울대학교병원, every other board -> its own hospital named after it.

-- ---------------------------------------------------------------------------
-- hospitals
-- ---------------------------------------------------------------------------
create table public.hospitals (
  id         bigint generated always as identity primary key,
  name       text not null check (length(btrim(name)) between 1 and 100),
  note       text not null default '' check (length(note) <= 1000),
  created_by uuid references public.profiles (id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  backfill_key text   -- used only by this migration, dropped below
);

create unique index hospitals_name_key on public.hospitals (lower(btrim(name)));
create index hospitals_created_by_idx on public.hospitals (created_by);

-- One hospital per group of boards. Runs of spaces in a name become one space (as the server
-- stores names). A name used by two groups gets " (2)", " (3)" appended: never the group key,
-- which holds the R-BAY agency code and could pass the 100-character limit.
with keyed as (
  select
    s.id,
    case
      when s.adapter = 'rbay' then 'rbay:' || coalesce(s.config ->> 'agency', s.id::text)
      when s.adapter = 'snuh_hrpp' then 'snuh_hrpp'
      else 'source:' || s.id
    end as k,
    case
      when s.adapter = 'snuh_hrpp' then '서울대학교병원'
      when s.adapter = 'rbay' and position(' : ' in s.name) > 1 then btrim(split_part(s.name, ' : ', 1))
      else btrim(s.name)
    end as hname
  from public.sources s
),
groups as (
  select distinct on (k) k, id as first_id,
         btrim(left(regexp_replace(hname, '\s+', ' ', 'g'), 80)) as hname
  from keyed
  order by k, id
),
numbered as (
  select k, hname,
         row_number() over (partition by lower(hname) order by first_id) as n
  from groups
),
named as (
  select k, case when n > 1 then hname || ' (' || n || ')' else hname end as hname
  from numbered
)
insert into public.hospitals (name, backfill_key)
select hname, k from named;

alter table public.sources
  add column hospital_id bigint references public.hospitals (id) on delete restrict,
  add column board_kind  text not null default 'other'
    check (board_kind in ('notice', 'forms', 'regulations', 'other'));

update public.sources s
set hospital_id = h.id
from public.hospitals h
where h.backfill_key = case
  when s.adapter = 'rbay' then 'rbay:' || coalesce(s.config ->> 'agency', s.id::text)
  when s.adapter = 'snuh_hrpp' then 'snuh_hrpp'
  else 'source:' || s.id
end;

update public.sources
set board_kind = case config ->> 'board'
  when 'NOTI' then 'notice'
  when 'FORM_BOX' then 'forms'
  when 'REGLA' then 'regulations'
  else 'other'
end
where adapter = 'rbay';

update public.sources
set board_kind = 'regulations'
where adapter = 'snuh_hrpp' and name ilike '%regulation%';

alter table public.sources alter column hospital_id set not null;
alter table public.hospitals drop column backfill_key;

create index sources_hospital_idx on public.sources (hospital_id);

-- ---------------------------------------------------------------------------
-- projects: hospitals instead of boards
-- ---------------------------------------------------------------------------
create table public.project_hospitals (
  project_id  bigint not null references public.projects (id) on delete cascade,
  hospital_id bigint not null references public.hospitals (id) on delete restrict,
  added_at    timestamptz not null default now(),
  primary key (project_id, hospital_id)
);

create index project_hospitals_hospital_idx on public.project_hospitals (hospital_id);

insert into public.project_hospitals (project_id, hospital_id, added_at)
select ps.project_id, s.hospital_id, min(ps.added_at)
from public.project_sources ps
join public.sources s on s.id = ps.source_id
group by ps.project_id, s.hospital_id;

alter table public.project_hospitals enable row level security;

revoke all on table public.project_hospitals from public, anon, authenticated, service_role;
grant select, delete on table public.project_hospitals to authenticated;
grant insert (project_id, hospital_id) on table public.project_hospitals to authenticated;
grant select, delete on table public.project_hospitals to service_role;

create policy "project_hospitals_select_own"
  on public.project_hospitals for select
  to authenticated
  using (exists (
    select 1 from public.projects p
    where p.id = project_id and p.user_id = (select auth.uid())
  ));

-- Any hospital may be added (owner, 2026-11-12); its boards stay gated per board.
create policy "project_hospitals_insert_own"
  on public.project_hospitals for insert
  to authenticated
  with check (exists (
    select 1 from public.projects p
    where p.id = project_id and p.user_id = (select auth.uid())
  ));

create policy "project_hospitals_delete_own"
  on public.project_hospitals for delete
  to authenticated
  using (exists (
    select 1 from public.projects p
    where p.id = project_id and p.user_id = (select auth.uid())
  ));

-- The 30-per-project limit, now on hospitals (same function, new table name).
create or replace function private.check_project_limits()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_table_name = 'projects' then
    if (select count(*) from public.projects where user_id = new.user_id) >= 50 then
      raise exception 'at most 50 projects per user' using errcode = 'P0001';
    end if;
  else
    if (select count(*) from public.project_hospitals where project_id = new.project_id) >= 30 then
      raise exception 'at most 30 hospitals per project' using errcode = 'P0001';
    end if;
  end if;
  return new;
end;
$$;

revoke all on function private.check_project_limits() from public, anon, authenticated;

create trigger check_project_hospital_limit
  before insert on public.project_hospitals
  for each row execute function private.check_project_limits();

drop table public.project_sources;

-- ---------------------------------------------------------------------------
-- pipelines: one live pipeline per project and hospital
-- ---------------------------------------------------------------------------
alter table public.pipelines
  add column hospital_id bigint references public.hospitals (id) on delete no action;

update public.pipelines p
set hospital_id = s.hospital_id
from public.sources s
where s.id = p.source_id;

-- Two live pipelines on boards of the same hospital in one project cannot be merged here.
do $$
begin
  if exists (
    select 1 from public.pipelines
    where deleted_at is null
    group by project_id, hospital_id
    having count(*) > 1
  ) then
    raise exception 'two live pipelines of one project point at boards of the same hospital; remove one first';
  end if;
end;
$$;

alter table public.pipelines alter column hospital_id set not null;
drop index public.pipelines_project_source_key;
drop index public.pipelines_source_idx;
alter table public.pipelines drop column source_id;

create unique index pipelines_project_hospital_key
  on public.pipelines (project_id, hospital_id) where deleted_at is null;
create index pipelines_hospital_idx on public.pipelines (hospital_id);

comment on table public.pipelines is
  'A user''s IRB pipeline for one hospital in one project. Owner only, served by the server.';

-- ---------------------------------------------------------------------------
-- pipeline_templates: per-hospital step order
-- ---------------------------------------------------------------------------
alter table public.pipeline_templates
  add column hospital_id bigint references public.hospitals (id) on delete cascade;

-- A hospital whose boards had several orders keeps the most recently saved one.
update public.pipeline_templates t
set hospital_id = s.hospital_id
from public.sources s
where s.id = t.source_id;

delete from public.pipeline_templates t
where t.hospital_id is not null
  and exists (
    select 1 from public.pipeline_templates o
    where o.hospital_id = t.hospital_id
      and (o.updated_at, o.id) > (t.updated_at, t.id)
  );

drop index public.pipeline_templates_source_key;
drop index public.pipeline_templates_global_key;
alter table public.pipeline_templates drop column source_id;

create unique index pipeline_templates_hospital_key
  on public.pipeline_templates (hospital_id) where hospital_id is not null;
create unique index pipeline_templates_global_key
  on public.pipeline_templates ((hospital_id is null)) where hospital_id is null;

comment on table public.pipeline_templates is
  'Default step order: one global row (hospital_id null) and optional per-hospital rows.';

-- ---------------------------------------------------------------------------
-- guides and form templates: linked to hospitals
-- ---------------------------------------------------------------------------
create table public.hospital_guides (
  hospital_id bigint not null references public.hospitals (id) on delete cascade,
  guide_id    bigint not null references public.guides (id) on delete restrict,
  created_at  timestamptz not null default now(),
  primary key (hospital_id, guide_id)
);

create index hospital_guides_guide_idx on public.hospital_guides (guide_id);

insert into public.hospital_guides (hospital_id, guide_id, created_at)
select s.hospital_id, sg.guide_id, min(sg.created_at)
from public.source_guides sg
join public.sources s on s.id = sg.source_id
group by s.hospital_id, sg.guide_id;

create table public.hospital_form_templates (
  hospital_id bigint not null references public.hospitals (id) on delete cascade,
  template_id bigint not null references public.form_templates (id) on delete restrict,
  created_at  timestamptz not null default now(),
  primary key (hospital_id, template_id)
);

create index hospital_form_templates_template_idx on public.hospital_form_templates (template_id);

insert into public.hospital_form_templates (hospital_id, template_id, created_at)
select s.hospital_id, sft.template_id, min(sft.created_at)
from public.source_form_templates sft
join public.sources s on s.id = sft.source_id
group by s.hospital_id, sft.template_id;

drop table public.source_guides;
drop table public.source_form_templates;

-- ---------------------------------------------------------------------------
-- Row level security and grants
-- ---------------------------------------------------------------------------
alter table public.hospitals               enable row level security;
alter table public.hospital_guides         enable row level security;
alter table public.hospital_form_templates enable row level security;

revoke all on table public.hospitals, public.hospital_guides, public.hospital_form_templates
from public, anon, authenticated, service_role;
-- The browser reads id and name only: the admin's note and created_by stay with the server.
grant select (id, name) on table public.hospitals to authenticated;
-- delete: only to undo a hospital made for a board that was then not created (the foreign key
-- from sources refuses a hospital that has boards).
grant select, insert, update, delete on table public.hospitals to service_role;
grant select, insert, delete on table public.hospital_guides, public.hospital_form_templates
  to service_role;

-- Every signed-in user may read the catalog (id and name only, see the column grant).
create policy "hospitals_select_authenticated"
  on public.hospitals for select
  to authenticated
  using (true);
-- hospital_guides and hospital_form_templates: no policy for the browser on purpose.

comment on table public.hospitals is
  'Platform-wide hospital catalog; groups boards (sources). Readable by every signed-in user.';
comment on column public.sources.hospital_id is 'The hospital this board belongs to.';
comment on column public.sources.board_kind is
  'notice, forms, regulations or other. Sets defaults only (e.g. pipeline step inputs).';
comment on table public.project_hospitals is
  'Hospitals in a project. Any hospital may be added; its boards stay gated per board.';
comment on table public.hospital_guides is 'Default guides of a hospital.';
comment on table public.hospital_form_templates is 'Form templates a hospital offers.';
