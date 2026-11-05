-- Fix: the previous migration granted service_role only select + insert on audit_logs,
-- but the project's default privileges had already given it every privilege on new
-- tables, so update/delete (and truncate) were still allowed. Revoke them explicitly.
revoke all on table public.audit_logs from service_role;
grant select, insert on table public.audit_logs to service_role;
