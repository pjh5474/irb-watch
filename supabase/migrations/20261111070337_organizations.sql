-- Organizations, step 1: columns and one default organization (owner, 2026-11-11; see
-- docs/decisions.md "Organizations (SaaS step 1)").
--
--   * organizations + organization_members (role owner | admin | member). A user may belong to
--     several organizations. profiles.role stays the server-wide (platform) admin flag.
--   * One organization, "Default", with a fixed id. Every existing user joins it (admins as
--     owner); every new profile joins it as member (trigger below).
--   * Public boards are shared by every organization: sources.organization_id is null.
--     Members-only (requires_login, so every R-BAY board) and manual sources belong to one
--     organization; a trigger fills the default organization and a check makes it required.
--     Documents, versions and files follow their source and get no column.
--   * User-made data gets organization_id not null, default = the default organization, so the
--     server needs no change yet.
--   * audit_logs gets a nullable column without a default: null means "before organizations"
--     (all of it the default organization) or a shared-source event.
--   * NOT enforced yet: no RLS policy and no server filter uses organization_id. The column
--     defaults must be removed when enforcement comes, so a missing organization fails loudly.

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------
create table public.organizations (
  id          uuid primary key default gen_random_uuid(),
  name        text not null check (length(btrim(name)) between 1 and 200),
  created_at  timestamptz not null default now()
);

create table public.organization_members (
  organization_id  uuid not null references public.organizations (id) on delete cascade,
  user_id          uuid not null references public.profiles (id) on delete cascade,
  role             text not null default 'member' check (role in ('owner', 'admin', 'member')),
  created_at       timestamptz not null default now(),
  primary key (organization_id, user_id)
);

create index organization_members_user_idx on public.organization_members (user_id);

insert into public.organizations (id, name)
values ('00000000-0000-4000-8000-000000000001', 'Default');

insert into public.organization_members (organization_id, user_id, role)
select '00000000-0000-4000-8000-000000000001',
       p.id,
       case when p.role = 'admin' then 'owner' else 'member' end
from public.profiles p;

alter table public.organizations        enable row level security;
alter table public.organization_members enable row level security;

revoke all on table public.organizations, public.organization_members
from public, anon, authenticated, service_role;
grant select, insert, update, delete on table
  public.organizations, public.organization_members
to service_role;
-- No policy for the browser yet: only the server reads these tables.

comment on table public.organizations is
  'Customer organizations. Step 1: one Default organization; nothing is filtered by it yet.';
comment on table public.organization_members is
  'Which user belongs to which organization, with an organization role. profiles.role stays the platform admin flag.';

-- Every new profile joins the default organization (until organizations are managed).
create function private.join_default_organization()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.organization_members (organization_id, user_id, role)
  values ('00000000-0000-4000-8000-000000000001', new.id, 'member')
  on conflict do nothing;
  return new;
end;
$$;

revoke all on function private.join_default_organization() from public, anon, authenticated;

create trigger join_default_organization
  after insert on public.profiles
  for each row execute function private.join_default_organization();

-- ---------------------------------------------------------------------------
-- sources: null = shared public board; members-only and manual sources are owned
-- ---------------------------------------------------------------------------
alter table public.sources
  add column organization_id uuid references public.organizations (id);

update public.sources
set organization_id = '00000000-0000-4000-8000-000000000001'
where requires_login or adapter = 'manual';

alter table public.sources
  add constraint sources_private_owned
  check (organization_id is not null or (not requires_login and adapter <> 'manual'));

create index sources_organization_idx on public.sources (organization_id);

-- Fills the default organization when a source becomes members-only or manual without one.
create function private.source_default_organization()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.organization_id is null and (new.requires_login or new.adapter = 'manual') then
    new.organization_id := '00000000-0000-4000-8000-000000000001';
  end if;
  return new;
end;
$$;

revoke all on function private.source_default_organization() from public, anon, authenticated;

create trigger source_default_organization
  before insert or update of requires_login, adapter, organization_id on public.sources
  for each row execute function private.source_default_organization();

comment on column public.sources.organization_id is
  'Null: a public board shared by every organization. Set: owned by one organization (members-only and manual sources must have one).';

-- ---------------------------------------------------------------------------
-- User-made data: owned by one organization
-- ---------------------------------------------------------------------------
alter table public.projects
  add column organization_id uuid not null
    default '00000000-0000-4000-8000-000000000001' references public.organizations (id);
alter table public.drafts
  add column organization_id uuid not null
    default '00000000-0000-4000-8000-000000000001' references public.organizations (id);
alter table public.guides
  add column organization_id uuid not null
    default '00000000-0000-4000-8000-000000000001' references public.organizations (id);
alter table public.form_templates
  add column organization_id uuid not null
    default '00000000-0000-4000-8000-000000000001' references public.organizations (id);
alter table public.chat_conversations
  add column organization_id uuid not null
    default '00000000-0000-4000-8000-000000000001' references public.organizations (id);
alter table public.ai_instructions
  add column organization_id uuid not null
    default '00000000-0000-4000-8000-000000000001' references public.organizations (id);
alter table public.notification_prefs
  add column organization_id uuid not null
    default '00000000-0000-4000-8000-000000000001' references public.organizations (id);
alter table public.subscriptions
  add column organization_id uuid not null
    default '00000000-0000-4000-8000-000000000001' references public.organizations (id);
alter table public.summary_requests
  add column organization_id uuid not null
    default '00000000-0000-4000-8000-000000000001' references public.organizations (id);
alter table public.site_credentials
  add column organization_id uuid not null
    default '00000000-0000-4000-8000-000000000001' references public.organizations (id);
-- Still exactly one settings row (id = 1); one row per organization comes with enforcement.
alter table public.agent_settings
  add column organization_id uuid not null unique
    default '00000000-0000-4000-8000-000000000001' references public.organizations (id);

alter table public.audit_logs
  add column organization_id uuid references public.organizations (id);

create index projects_organization_idx           on public.projects (organization_id);
create index drafts_organization_idx             on public.drafts (organization_id);
create index guides_organization_idx             on public.guides (organization_id);
create index form_templates_organization_idx     on public.form_templates (organization_id);
create index chat_conversations_organization_idx on public.chat_conversations (organization_id);
create index ai_instructions_organization_idx    on public.ai_instructions (organization_id);
create index notification_prefs_organization_idx on public.notification_prefs (organization_id);
create index subscriptions_organization_idx      on public.subscriptions (organization_id);
create index summary_requests_organization_idx   on public.summary_requests (organization_id);
create index site_credentials_organization_idx   on public.site_credentials (organization_id);
create index audit_logs_organization_idx         on public.audit_logs (organization_id, created_at desc);

-- Browsers insert their own subscriptions. The table-wide insert grant would let them pick any
-- organization_id; limit it to the two columns they set today.
revoke insert on table public.subscriptions from authenticated;
grant insert (user_id, source_id) on table public.subscriptions to authenticated;
