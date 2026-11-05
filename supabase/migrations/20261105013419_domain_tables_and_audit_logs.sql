-- Domain tables, audit log, admin helper, audit triggers and the private files bucket.
--
-- Access model
--   * The FastAPI server uses the service role for all writes.
--   * Signed-in users read through supabase-js under RLS: the source catalog, plus
--     documents / versions / document-change log entries of sources they subscribe to.
--   * Admins (profiles.role = 'admin') can read everything.
--   * Nobody but the service role can touch Storage objects; the server hands out
--     short-lived signed URLs after checking access.

-- ---------------------------------------------------------------------------
-- Admin helper (lives in the non-exposed `private` schema)
-- ---------------------------------------------------------------------------
create function private.is_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.profiles
    where id = (select auth.uid()) and role = 'admin'
  );
$$;

revoke all on function private.is_admin() from public, anon;
grant execute on function private.is_admin() to authenticated;
grant usage on schema private to authenticated;

-- Admins may list every profile (users manage themselves via profiles_select_own).
create policy "profiles_select_admin"
  on public.profiles for select
  to authenticated
  using ((select private.is_admin()));

-- ---------------------------------------------------------------------------
-- sources: admin-managed list of URLs to scan
-- ---------------------------------------------------------------------------
create table public.sources (
  id               bigint generated always as identity primary key,
  name             text not null,
  adapter          text not null check (adapter in ('snuh_hrpp')),
  url              text not null unique,
  config           jsonb not null default '{}'::jsonb,   -- adapter settings, e.g. {"board_key": "2605277029"}
  needs_browser    boolean not null default false,
  is_active        boolean not null default true,
  last_scan_at     timestamptz,
  last_scan_status text check (last_scan_status in ('ok', 'needs_review', 'failed')),
  last_scan_error  text,
  created_by       uuid references public.profiles (id) on delete set null,
  created_at       timestamptz not null default now()
);

create index sources_created_by_idx on public.sources (created_by);

-- ---------------------------------------------------------------------------
-- documents: one row per post on a source (current state)
-- ---------------------------------------------------------------------------
create table public.documents (
  id             bigint generated always as identity primary key,
  source_id      bigint not null references public.sources (id) on delete cascade,
  external_id    text not null,                 -- post ID on the site, e.g. 2609030006
  title          text not null,
  source_url     text not null,                 -- link to the original post page
  posted_on      date,
  status         text not null default 'current'
                   check (status in ('current', 'removed', 'needs_review')),
  miss_count     smallint not null default 0,   -- consecutive scans where the post was missing
  first_seen_at  timestamptz not null default now(),
  last_seen_at   timestamptz,
  removed_at     timestamptz,
  unique (source_id, external_id),
  unique (id, source_id)                        -- lets versions prove they match the document's source
);

create index documents_source_status_idx on public.documents (source_id, status);

-- ---------------------------------------------------------------------------
-- document_versions: every distinct state of a document (text + attached files)
-- ---------------------------------------------------------------------------
create table public.document_versions (
  id           bigint generated always as identity primary key,
  document_id  bigint not null,
  source_id    bigint not null,                 -- denormalised so RLS needs no join
  version_no   integer not null,
  change_type  text not null check (change_type in ('baseline', 'updated')),
  title        text not null,
  body_hash    text not null,
  body_text    text not null,
  -- [{"filename": "...", "size_label": "66 KB", "sha256": "...", "storage_path": "..."}]
  files        jsonb not null default '[]'::jsonb,
  summary      text,                            -- AI summary of what changed
  priority     text check (priority in ('low', 'medium', 'high')),
  created_at   timestamptz not null default now(),
  unique (document_id, version_no),
  foreign key (document_id, source_id)
    references public.documents (id, source_id) on delete cascade
);

create index document_versions_source_created_idx
  on public.document_versions (source_id, created_at desc);

-- ---------------------------------------------------------------------------
-- subscriptions: which user follows which source
-- ---------------------------------------------------------------------------
create table public.subscriptions (
  user_id     uuid not null references public.profiles (id) on delete cascade,
  source_id   bigint not null references public.sources (id) on delete cascade,
  created_at  timestamptz not null default now(),
  primary key (user_id, source_id)
);

create index subscriptions_source_idx on public.subscriptions (source_id);

-- ---------------------------------------------------------------------------
-- audit_logs: append-only event log; also feeds dashboard news cards
--   category: document | scan | admin | user
--   event_type examples: document.new, document.updated, document.removed,
--     scan.finished, scan.needs_review, source.created, user.created,
--     role.changed, subscription.added, subscription.removed
-- No foreign keys on purpose: entries must outlive the rows they describe.
-- ---------------------------------------------------------------------------
create table public.audit_logs (
  id           bigint generated always as identity primary key,
  created_at   timestamptz not null default now(),
  category     text not null check (category in ('document', 'scan', 'admin', 'user')),
  event_type   text not null,
  actor_id     uuid,                            -- null when done by the system / service role
  source_id    bigint,
  document_id  bigint,
  title        text,                            -- news-card headline
  summary      text,                            -- news-card body (AI summary)
  priority     text check (priority in ('low', 'medium', 'high')),
  payload      jsonb not null default '{}'::jsonb
);

create index audit_logs_created_idx on public.audit_logs (created_at desc);
create index audit_logs_category_created_idx on public.audit_logs (category, created_at desc);
create index audit_logs_document_feed_idx
  on public.audit_logs (source_id, created_at desc) where category = 'document';

-- ---------------------------------------------------------------------------
-- Row level security + grants
-- ---------------------------------------------------------------------------
alter table public.sources           enable row level security;
alter table public.documents         enable row level security;
alter table public.document_versions enable row level security;
alter table public.subscriptions     enable row level security;
alter table public.audit_logs        enable row level security;

revoke all on table
  public.sources, public.documents, public.document_versions,
  public.subscriptions, public.audit_logs
from anon, authenticated;

grant select on table
  public.sources, public.documents, public.document_versions, public.audit_logs
to authenticated;
grant select, insert, delete on table public.subscriptions to authenticated;

grant select, insert, update, delete on table
  public.sources, public.documents, public.document_versions, public.subscriptions
to service_role;
-- The audit log is append-only: nobody, including the service role, may edit or delete it.
grant select, insert on table public.audit_logs to service_role;
grant usage on all sequences in schema public to service_role;

-- sources: every signed-in user can browse the catalog to decide what to follow.
create policy "sources_select_authenticated"
  on public.sources for select
  to authenticated
  using (true);

-- documents / versions: only for subscribed sources, or everything for admins.
create policy "documents_select_subscribed_or_admin"
  on public.documents for select
  to authenticated
  using (
    (select private.is_admin())
    or source_id in (
      select s.source_id from public.subscriptions s where s.user_id = (select auth.uid())
    )
  );

create policy "document_versions_select_subscribed_or_admin"
  on public.document_versions for select
  to authenticated
  using (
    (select private.is_admin())
    or source_id in (
      select s.source_id from public.subscriptions s where s.user_id = (select auth.uid())
    )
  );

-- subscriptions: users manage only their own.
create policy "subscriptions_select_own"
  on public.subscriptions for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy "subscriptions_insert_own"
  on public.subscriptions for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

create policy "subscriptions_delete_own"
  on public.subscriptions for delete
  to authenticated
  using ((select auth.uid()) = user_id);

-- audit_logs: users see document-change entries of subscribed sources; admins see all.
create policy "audit_logs_select_documents_subscribed_or_admin"
  on public.audit_logs for select
  to authenticated
  using (
    (select private.is_admin())
    or (
      category = 'document'
      and source_id in (
        select s.source_id from public.subscriptions s where s.user_id = (select auth.uid())
      )
    )
  );

-- ---------------------------------------------------------------------------
-- Audit triggers for changes that happen outside the server's own logging
-- ---------------------------------------------------------------------------
create function private.audit_subscription_change()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if tg_op = 'INSERT' then
    insert into public.audit_logs (category, event_type, actor_id, source_id)
    values ('user', 'subscription.added', new.user_id, new.source_id);
    return new;
  end if;
  insert into public.audit_logs (category, event_type, actor_id, source_id)
  values ('user', 'subscription.removed', old.user_id, old.source_id);
  return old;
end;
$$;

revoke all on function private.audit_subscription_change() from public, anon, authenticated;

create trigger audit_subscription_change
  after insert or delete on public.subscriptions
  for each row execute function private.audit_subscription_change();

create function private.audit_role_change()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.audit_logs (category, event_type, actor_id, payload)
  values (
    'admin', 'role.changed', (select auth.uid()),
    jsonb_build_object('user_id', new.id, 'from', old.role, 'to', new.role)
  );
  return new;
end;
$$;

revoke all on function private.audit_role_change() from public, anon, authenticated;

create trigger audit_role_change
  after update of role on public.profiles
  for each row
  when (old.role is distinct from new.role)
  execute function private.audit_role_change();

-- ---------------------------------------------------------------------------
-- Private bucket for original files. No storage.objects policies on purpose:
-- with RLS on and no policy, only the service role can read or write objects.
-- ---------------------------------------------------------------------------
insert into storage.buckets (id, name, public, file_size_limit)
values ('documents', 'documents', false, 20000000)
on conflict (id) do nothing;
