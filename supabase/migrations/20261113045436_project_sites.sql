-- Sites per PI (IRB pipeline redesign, phase B2a; see docs/pipeline-design.md 4.1 and
-- docs/decisions.md "IRB pipeline redesign, phase B2a: sites per PI").
--
-- A site is one IRB track of a project: a hospital (the IRB it submits to), usually a PI and a
-- site number; PI and number may be empty (most projects have one IRB and no site folder). Each
-- site has at most one live pipeline. Other site details (CRA, CRC, Sub-I, 연구비, ...) are free
-- facts with a dated history: rows are only ever added; a correction is a new row naming the
-- row it replaces. The fact name "PI" is reserved: a PI change writes the PI fact and, once it
-- applies, pi_name (public.set_site_pi; a later-dated change is applied by the daily job,
-- public.apply_due_site_pi). The PI a site is created with is dated 1900-01-01 ("from the
-- start"), so a change dated back still counts after it.
--
-- Both tables are server-only (RLS on, no browser grants): the server serves whoever may open
-- the project (phase B1's rule).

-- ---------------------------------------------------------------------------
-- project_sites
-- ---------------------------------------------------------------------------
create table public.project_sites (
  id               bigint generated always as identity primary key,
  -- A site belongs to its project: deleting a project deletes its sites. A site with a
  -- pipeline still blocks that (pipelines -> projects is NO ACTION).
  project_id       bigint not null references public.projects (id) on delete cascade,
  hospital_id      bigint not null references public.hospitals (id) on delete no action,
  user_id          uuid references public.profiles (id) on delete set null,  -- who made it
  organization_id  uuid not null default '00000000-0000-4000-8000-000000000001'
                     references public.organizations (id),
  site_no          text not null default '' check (char_length(site_no) <= 20),
  pi_name          text not null default '' check (char_length(pi_name) <= 100),
  note             text not null default '' check (char_length(note) <= 4000),
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  deleted_at       timestamptz,
  unique (id, project_id, hospital_id)  -- the target of the pipelines foreign key
);

-- One PI name once per hospital of a project (an empty name too: one unnamed site), and a site
-- number, when given, once per project.
create unique index project_sites_pi_key
  on public.project_sites (project_id, hospital_id, lower(btrim(pi_name))) where deleted_at is null;
create unique index project_sites_no_key
  on public.project_sites (project_id, lower(btrim(site_no))) where deleted_at is null and btrim(site_no) <> '';
create index project_sites_hospital_idx on public.project_sites (hospital_id);

create table public.site_facts (
  id          bigint generated always as identity primary key,
  site_id     bigint not null references public.project_sites (id) on delete cascade,
  name        text   not null check (char_length(btrim(name)) between 1 and 100),
  value       text   not null check (char_length(value) <= 2000),   -- '' = the fact was removed
  valid_from  date   not null,                                      -- the day the value applies
  step_id     bigint references public.pipeline_steps (id) on delete set null,  -- the step that reported it
  corrects    bigint references public.site_facts (id) on delete set null,      -- a typo fix replaces that row
  changed_by  uuid references public.profiles (id) on delete set null,
  created_at  timestamptz not null default now()
);

create index site_facts_site_name_idx on public.site_facts (site_id, lower(btrim(name)), valid_from desc, id desc);
create index site_facts_step_idx on public.site_facts (step_id) where step_id is not null;

alter table public.project_sites enable row level security;
alter table public.site_facts enable row level security;

-- The project's default privileges give service_role everything: revoke first, then grant
-- exactly what is needed. No browser access and no policies: the server serves them.
revoke all on table public.project_sites from public, anon, authenticated, service_role;
revoke all on table public.site_facts from public, anon, authenticated, service_role;
grant select, insert, update, delete on table public.project_sites to service_role;  -- delete: the purge
grant select, insert on table public.site_facts to service_role;                     -- history: add only

comment on table public.project_sites is
  'One IRB track of a project: hospital, PI (optional), site number (optional). Server only.';
comment on table public.site_facts is
  'Dated history of a site''s details. Rows are only added; a correction names the row it replaces.';

-- updated_at follows every change (the same function as projects).
create trigger touch_project_site
  before update on public.project_sites
  for each row execute function private.touch_project();

-- A site's organization is its project's (an access column), never chosen by a caller.
create function private.set_site_organization()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  select p.organization_id into new.organization_id from public.projects p where p.id = new.project_id;
  return new;
end;
$$;

revoke all on function private.set_site_organization() from public, anon, authenticated;

create trigger set_site_organization
  before insert on public.project_sites
  for each row execute function private.set_site_organization();

-- ---------------------------------------------------------------------------
-- pipelines: one live pipeline per site
-- ---------------------------------------------------------------------------
alter table public.pipelines add column site_id bigint;

-- Backfill: one unnamed site per project and hospital that has pipelines (live or removed),
-- created by the oldest pipeline's starter.
insert into public.project_sites (project_id, hospital_id, user_id, created_at)
select p.project_id, p.hospital_id,
       (array_agg(p.user_id order by p.created_at))[1],
       min(p.created_at)
from public.pipelines p
group by p.project_id, p.hospital_id;

update public.pipelines p
set site_id = s.id
from public.project_sites s
where s.project_id = p.project_id and s.hospital_id = p.hospital_id and s.deleted_at is null and s.pi_name = '';

alter table public.pipelines alter column site_id set not null;

-- A pipeline can never disagree with its site about project or hospital.
alter table public.pipelines
  add constraint pipelines_site_fkey
  foreign key (site_id, project_id, hospital_id)
  references public.project_sites (id, project_id, hospital_id) on delete no action;

drop index public.pipelines_project_hospital_key;
create unique index pipelines_site_key on public.pipelines (site_id) where deleted_at is null;

-- An insert without a site (the previous server version, during a deploy) uses the hospital's
-- unnamed site, made when missing.
create function private.default_pipeline_site()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  found bigint;
begin
  if new.site_id is not null then
    return new;
  end if;
  select s.id into found from public.project_sites s
  where s.project_id = new.project_id and s.hospital_id = new.hospital_id
    and s.deleted_at is null and btrim(s.pi_name) = ''
  limit 1;
  if found is null then
    insert into public.project_sites (project_id, hospital_id, user_id)
    values (new.project_id, new.hospital_id, new.user_id)
    returning id into found;
  end if;
  new.site_id = found;
  return new;
end;
$$;

revoke all on function private.default_pipeline_site() from public, anon, authenticated;

create trigger default_pipeline_site
  before insert on public.pipelines
  for each row execute function private.default_pipeline_site();

comment on column public.pipelines.site_id is
  'The site (hospital + PI) this pipeline is the IRB lifecycle of. One live pipeline per site.';

-- ---------------------------------------------------------------------------
-- A PI change (server only). The PI fact row always; pi_name too when it applies now
-- (p_apply). A change dated later is scheduled: public.apply_due_site_pi switches pi_name on
-- that day (the daily job). A name another site of the hospital already uses raises 23505.
-- ---------------------------------------------------------------------------
create function public.set_site_pi(
  p_site bigint, p_name text, p_valid_from date, p_step bigint, p_actor uuid, p_apply boolean
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  if p_apply then
    update public.project_sites set pi_name = btrim(p_name) where id = p_site;
  else
    perform 1 from public.project_sites where id = p_site;
  end if;
  if not found then
    raise exception 'site not found' using errcode = 'P0002';
  end if;
  insert into public.site_facts (site_id, name, value, valid_from, step_id, changed_by)
  values (p_site, 'PI', btrim(p_name), p_valid_from, p_step, p_actor);
end;
$$;

-- The daily job: every live site whose PI in force on p_today (the newest PI row dated that day
-- or earlier) differs from pi_name gets it. One site whose new name clashes is skipped (and
-- counted), the others still change. Returns (changed, skipped).
create function public.apply_due_site_pi(p_today date, out changed int, out skipped int)
language plpgsql
security definer
set search_path = ''
as $$
declare
  r record;
begin
  changed := 0;
  skipped := 0;
  for r in
    select s.id, f.value
    from public.project_sites s
    join lateral (
      select x.value from public.site_facts x
      where x.site_id = s.id and lower(btrim(x.name)) = 'pi' and x.valid_from <= p_today
        and not exists (select 1 from public.site_facts c where c.corrects = x.id)
      order by x.valid_from desc, x.id desc
      limit 1
    ) f on true
    where s.deleted_at is null and btrim(f.value) <> btrim(s.pi_name)
  loop
    begin
      update public.project_sites set pi_name = btrim(r.value) where id = r.id;
      changed := changed + 1;
    exception when unique_violation then
      skipped := skipped + 1;
    end;
  end loop;
end;
$$;

revoke all on function public.set_site_pi(bigint, text, date, bigint, uuid, boolean) from public, anon, authenticated;
revoke all on function public.apply_due_site_pi(date) from public, anon, authenticated;
grant execute on function public.set_site_pi(bigint, text, date, bigint, uuid, boolean) to service_role;
grant execute on function public.apply_due_site_pi(date) to service_role;
