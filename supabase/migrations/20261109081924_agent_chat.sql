-- The assistant ("agent"): one chat history per user, and the admin-set daily limits.
--
-- Owner decisions 2026-11-09:
--   * Each user has their own conversations (a list; start new, switch, delete). Nobody reads
--     another user's chats, not even an admin through the browser.
--   * Conversations are deleted 30 days after their last message (done by the server, daily).
--   * The daily limit is set by an admin on the admin page: one number for users, one for admins.
--     Starting values 30 and 100. The count itself is kept in audit_logs (agent.message), like
--     download links, so deleting a chat never resets a user's count.
--
-- Everything is written by the server (service role). The browser may only read its own rows.

-- ---------------------------------------------------------------------------
-- chat_conversations
-- ---------------------------------------------------------------------------
create table public.chat_conversations (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references public.profiles (id) on delete cascade,
  title      text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index chat_conversations_user_idx
  on public.chat_conversations (user_id, updated_at desc);
create index chat_conversations_updated_idx
  on public.chat_conversations (updated_at);

-- ---------------------------------------------------------------------------
-- chat_messages
--   user_id repeats the owner so the read policy needs no join.
--   actions: confirm-button cards the assistant proposed to an admin (data only; the browser
--            runs an action through the normal admin API after the admin clicks Confirm).
-- ---------------------------------------------------------------------------
create table public.chat_messages (
  id              bigint generated always as identity primary key,
  conversation_id uuid not null references public.chat_conversations (id) on delete cascade,
  user_id         uuid not null references public.profiles (id) on delete cascade,
  role            text not null check (role in ('user', 'assistant')),
  content         text not null,
  actions         jsonb not null default '[]'::jsonb,
  created_at      timestamptz not null default now()
);

create index chat_messages_conversation_idx
  on public.chat_messages (conversation_id, id);
create index chat_messages_user_idx on public.chat_messages (user_id);

-- ---------------------------------------------------------------------------
-- agent_settings: exactly one row
-- ---------------------------------------------------------------------------
create table public.agent_settings (
  id                 smallint primary key default 1 check (id = 1),
  user_daily_limit   integer not null default 30  check (user_daily_limit between 0 and 1000),
  admin_daily_limit  integer not null default 100 check (admin_daily_limit between 0 and 1000),
  updated_by         uuid references public.profiles (id) on delete set null,
  updated_at         timestamptz not null default now()
);

insert into public.agent_settings (id) values (1);

create index agent_settings_updated_by_idx on public.agent_settings (updated_by);

-- ---------------------------------------------------------------------------
-- Row level security and grants
-- The project's default privileges give service_role everything: revoke first, then grant
-- exactly what is needed.
-- ---------------------------------------------------------------------------
alter table public.chat_conversations enable row level security;
alter table public.chat_messages      enable row level security;
alter table public.agent_settings     enable row level security;

revoke all on table
  public.chat_conversations, public.chat_messages, public.agent_settings
from public, anon, authenticated, service_role;

grant select on table public.chat_conversations, public.chat_messages to authenticated;

grant select, insert, update, delete on table public.chat_conversations to service_role;
-- Messages are written once and never edited; they go away with their conversation (cascade).
grant select, insert on table public.chat_messages to service_role;
-- The settings row exists from this migration; the server only reads and changes it.
grant select, update on table public.agent_settings to service_role;

create policy "chat_conversations_select_own"
  on public.chat_conversations for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy "chat_messages_select_own"
  on public.chat_messages for select
  to authenticated
  using ((select auth.uid()) = user_id);

-- agent_settings: no policy for the browser on purpose. Admins read and change it through the
-- API, which checks the admin role.

comment on table public.chat_conversations is
  'One assistant conversation of one user. Deleted by the server 30 days after updated_at.';
comment on table public.chat_messages is
  'Messages of a conversation. actions = confirm-button proposals for admins (data only).';
comment on table public.agent_settings is
  'Single row: the daily message limits of the assistant, set by an admin.';
