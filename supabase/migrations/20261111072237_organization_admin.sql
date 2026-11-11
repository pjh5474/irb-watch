-- Organization admin page and statistics (owner, 2026-11-11; see docs/decisions.md
-- "Organization admin page and statistics").
--
--   * Soft delete: organizations.deleted_at. Names are unique among live organizations.
--   * Usage counts where it happened: every audit row written by a user is stamped with that
--     user's organization at insert time (trigger below), so moving a user never changes past
--     numbers. Rows written before this migration have no organization and count as Default.
--   * public.organization_usage(): counts per organization, user and event type in a time
--     window, for the server only.

alter table public.organizations add column deleted_at timestamptz;

create unique index organizations_live_name_key
  on public.organizations (lower(btrim(name)))
  where deleted_at is null;

-- Stamp each user's audit row with their organization (one organization per user).
create function private.audit_log_organization()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.organization_id is null and new.actor_id is not null then
    select m.organization_id into new.organization_id
    from public.organization_members m
    where m.user_id = new.actor_id;
  end if;
  return new;
end;
$$;

revoke all on function private.audit_log_organization() from public, anon, authenticated;

create trigger audit_log_organization
  before insert on public.audit_logs
  for each row execute function private.audit_log_organization();

-- Counts of user actions per organization, user and event type in [p_start, p_end).
create function public.organization_usage(
  p_start timestamptz,
  p_end   timestamptz,
  p_types text[]
)
returns table (organization_id uuid, actor_id uuid, event_type text, n bigint)
language sql
stable
security invoker
set search_path = ''
as $$
  select coalesce(a.organization_id, '00000000-0000-4000-8000-000000000001'::uuid),
         a.actor_id,
         a.event_type,
         count(*)
  from public.audit_logs a
  where a.created_at >= p_start
    and a.created_at < p_end
    and a.actor_id is not null
    and a.event_type = any (p_types)
  group by 1, 2, 3;
$$;

revoke all on function public.organization_usage(timestamptz, timestamptz, text[])
  from public, anon, authenticated;
grant execute on function public.organization_usage(timestamptz, timestamptz, text[])
  to service_role;
