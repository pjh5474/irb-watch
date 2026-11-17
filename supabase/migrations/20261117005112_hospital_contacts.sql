-- Hospital IRB contacts (owner, 2026-11-17; see docs/decisions.md "Hospital contacts").
--
-- One row per hospital with the phone, fax and email text of its IRB office:
--   * scanned_*  what the daily R-BAY scan read from the hospital's start page (the cards
--                전화, 팩스, 이메일), as the hospital wrote it, lines separated by a line break;
--   * manual_*   what a platform admin typed. A typed field wins on screen and the scan never
--                touches it; clearing it shows the scanned value again.
-- The last check is kept with its result: ok, or failed with a short kind (shown to everyone)
-- and the raw detail (admins only). A failed check never changes the saved texts.
--
-- Platform-wide on purpose, like `hospitals` and `irb_sessions`: one hospital, one contact
-- list for every customer. Readable by every signed-in user (owner); written by the server only.
-- error_detail, staff_error_detail and updated_by are not readable from the browser.
-- Staff names and work emails are personal work contacts read from a members-only page; the
-- owner decided every signed-in user may read them (2026-11-17).

create table public.hospital_contacts (
  hospital_id   bigint primary key references public.hospitals (id) on delete cascade,
  scanned_phone text not null default '' check (length(scanned_phone) <= 500),
  scanned_fax   text not null default '' check (length(scanned_fax) <= 500),
  scanned_email text not null default '' check (length(scanned_email) <= 500),
  manual_phone  text check (manual_phone is null or length(manual_phone) <= 500),
  manual_fax    text check (manual_fax is null or length(manual_fax) <= 500),
  manual_email  text check (manual_email is null or length(manual_email) <= 500),
  check_status  text not null default 'never' check (check_status in ('never', 'ok', 'failed')),
  checked_at    timestamptz,
  last_ok_at    timestamptz,
  error_kind    text check (error_kind is null or error_kind in
                  ('login_rejected', 'site_unavailable', 'layout_changed', 'other')),
  error_detail  text check (error_detail is null or length(error_detail) <= 500),
  -- The manager page ("업무별 담당자": people cards by tab), read only for a hospital whose start
  -- page lists it: [{"tab": "IRB", "entries": [{"title", "name", "email", "tel"}]}].
  -- staff_status: never (not read yet), none (the hospital has no manager page), ok, failed (the
  -- saved staff is then the last good read). Scanned only; admins do not type it.
  staff             jsonb not null default '[]' check (jsonb_typeof(staff) = 'array' and length(staff::text) <= 60000),
  staff_status      text not null default 'never' check (staff_status in ('never', 'none', 'ok', 'failed')),
  staff_error_kind  text check (staff_error_kind is null or staff_error_kind in
                      ('login_rejected', 'site_unavailable', 'layout_changed', 'other')),
  staff_error_detail text check (staff_error_detail is null or length(staff_error_detail) <= 500),
  updated_by    uuid references public.profiles (id) on delete set null,
  updated_at    timestamptz not null default now()
);

create index hospital_contacts_updated_by_idx on public.hospital_contacts (updated_by);

alter table public.hospital_contacts enable row level security;

revoke all on table public.hospital_contacts from public, anon, authenticated, service_role;
grant select (
  hospital_id, scanned_phone, scanned_fax, scanned_email, manual_phone, manual_fax, manual_email,
  check_status, checked_at, last_ok_at, error_kind, updated_at, staff, staff_status, staff_error_kind
) on table public.hospital_contacts to authenticated;
grant select, insert, update, delete on table public.hospital_contacts to service_role;

create policy "hospital_contacts_select_authenticated"
  on public.hospital_contacts for select
  to authenticated
  using (true);

comment on table public.hospital_contacts is
  'IRB office contacts per hospital: read from R-BAY by the scan, or typed by an admin (manual wins). Platform-wide; every signed-in user reads it; the server writes it.';
comment on column public.hospital_contacts.error_detail is 'Raw reason of the last failed check; admins only (not granted to the browser).';
comment on column public.hospital_contacts.staff_error_detail is 'Raw reason of the last failed manager-page read; admins only (not granted to the browser).';
