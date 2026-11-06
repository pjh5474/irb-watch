-- Hospital logins the server uses to scan members-only boards (CREDENTIAL_STORE=database).
--
-- One row per (owner_label, site), the same pair the Windows-only keyring store uses, for
-- example ('owner', 'snuh-2605277031'). The ID and password are stored ONLY as ciphertext
-- (Fernet, key in the CREDENTIALS_KEY env var, never in the database). Nobody but the
-- server (service_role) can touch this table: signed-in users and anonymous callers get no
-- privileges and there are no policies.

create table public.site_credentials (
  owner_label text not null check (owner_label ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
  site        text not null check (site ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
  ciphertext  text not null,
  updated_at  timestamptz not null default now(),
  updated_by  uuid references public.profiles (id) on delete set null,
  primary key (owner_label, site)
);

alter table public.site_credentials enable row level security;

revoke all on table public.site_credentials from anon, authenticated, service_role;
grant select, insert, update, delete on table public.site_credentials to service_role;

comment on table public.site_credentials is
  'Encrypted hospital logins for scanning members-only boards. service_role only; ciphertext is Fernet, key in CREDENTIALS_KEY.';
